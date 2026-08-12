from __future__ import annotations

import hashlib
import importlib
import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from leanfaith.cli.app import app
from leanfaith.config.hashing import canonical_json_bytes, hash_canonical
from leanfaith.models import m0_dual_encoder as m0
from leanfaith.models import m2_bidirectional_matcher as m2
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


def _m2_protocol() -> m2.ExperimentalM2ProxyProtocolConfig:
    return m2.load_experimental_m2_proxy_config(
        Path("configs/models/experimental_m2_bidirectional_matcher_proxy_v1.yaml")
    ).config


def _example(
    index: int,
    *,
    split: m0.ProxySplit,
    target: m0.ProxyTarget,
    private: bool = False,
) -> m0.M0ProxyExample:
    source = f"[HEADLESS]\n(n : Nat) : n + {index % 3} = n + {index % 3}"
    candidate = f"[HEADLESS]\n(m : Nat) : m + {index % 5} = m + {index % 5}"
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


def test_m2_protocol_reuses_frozen_m0_dependencies_and_disables_unsupported_heads() -> None:
    zero = _m0_protocol()
    two = _m2_protocol()

    assert two.backbone == zero.backbone
    assert two.training == zero.training
    assert two.tokenizer_audit_profile_id == zero.tokenizer_audit_profile_id
    assert two.architecture.matching_layer_count == 2
    assert two.architecture.directional_parameters_shared is True
    assert two.architecture.synchronous_directional_updates is True
    assert two.architecture.relation_head_enabled is False
    assert two.architecture.ambiguity_head_enabled is False
    assert two.semantic_prediction is False
    assert two.model_selection_eligible is False


def test_m2_module_is_exactly_swap_invariant_and_uses_shared_directional_layers() -> None:
    torch = pytest.importorskip("torch")

    class TinyEncoder(torch.nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.embedding = torch.nn.Embedding(128, 8)
            self.calls = 0

        def forward(self, *, input_ids: Any, attention_mask: Any) -> object:
            del attention_mask
            self.calls += 1
            return SimpleNamespace(last_hidden_state=self.embedding(input_ids))

    torch.manual_seed(99)
    encoder = TinyEncoder()
    model = m2.build_m2_bidirectional_matcher_module(
        encoder=encoder,
        hidden_size=8,
        attention_head_count=2,
    )
    model.eval()
    source_ids = torch.tensor([[1, 2, 3, 0], [5, 6, 0, 0]])
    source_mask = torch.tensor([[1, 1, 1, 0], [1, 1, 0, 0]])
    candidate_ids = torch.tensor([[8, 9, 0], [10, 11, 12]])
    candidate_mask = torch.tensor([[1, 1, 0], [1, 1, 1]])

    direct = model(
        source_input_ids=source_ids,
        source_attention_mask=source_mask,
        candidate_input_ids=candidate_ids,
        candidate_attention_mask=candidate_mask,
    )
    swapped = model(
        source_input_ids=candidate_ids,
        source_attention_mask=candidate_mask,
        candidate_input_ids=source_ids,
        candidate_attention_mask=source_mask,
    )

    assert model.encoder is encoder
    assert len(model.matching_layers) == 2
    assert encoder.calls == 4
    assert torch.equal(direct["symmetric_features"], swapped["symmetric_features"])
    assert torch.equal(direct["logits"], swapped["logits"])
    assert torch.equal(direct["probabilities"], swapped["probabilities"])
    # A cached base encoding can be reused, while candidate-dependent matching
    # is still explicitly executed by match_encoded.
    source_hidden = model.encode_base(input_ids=source_ids, attention_mask=source_mask)
    candidate_hidden = model.encode_base(input_ids=candidate_ids, attention_mask=candidate_mask)
    cached = model.match_encoded(
        source_hidden=source_hidden,
        source_attention_mask=source_mask,
        candidate_hidden=candidate_hidden,
        candidate_attention_mask=candidate_mask,
    )
    assert torch.equal(direct["logits"], cached["logits"])


def test_tiny_m2_training_replays_and_exact_verifier_rechecks_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    torch = pytest.importorskip("torch")
    examples = _examples()
    protocol = _m2_protocol()
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
            self.embedding = torch.nn.Embedding(128, 8)

        def forward(self, *, input_ids: Any, attention_mask: Any) -> object:
            del attention_mask
            return SimpleNamespace(last_hidden_state=self.embedding(input_ids))

    def runtime() -> m2.LoadedM2ProxyRuntime:
        torch.manual_seed(101)
        model = m2.build_m2_bidirectional_matcher_module(
            encoder=TinyEncoder(), hidden_size=8, attention_head_count=2
        )
        return m2.LoadedM2ProxyRuntime(
            model=model,
            tokenizer=tokenizer,
            selected_length=512,
            checkpoint=checkpoint,
            audited_tokenizer_snapshot=snapshot,
            protocol=protocol,
            initial_model_state_sha256=hashlib.sha256(
                m0._safetensors_state_bytes(model, module_importer=importlib.import_module)
            ).hexdigest(),
            prepared_tokenization_sha256=m0._prepared_tokenization_sha256(tokenizer, examples),
        )

    repository = tmp_path / "repository"
    repository.mkdir()
    monkeypatch.setattr(m2, "verify_experimental_m0_proxy_inputs", lambda _: input_manifest)
    monkeypatch.setattr(m2, "_load_prepared_examples", lambda _: examples)
    monkeypatch.setattr(m2, "_load_audited_tokenizer", lambda _: tokenizer)
    monkeypatch.setattr(m2, "_verify_audited_tokenizer_snapshot", lambda _: tmp_path)
    monkeypatch.setattr(m2, "verify_local_modernbert_checkpoint", lambda _: checkpoint_dir)
    monkeypatch.setattr(m2, "collect_code_state", lambda _: _clean_code())
    monkeypatch.setattr(m2, "load_m2_proxy_runtime", lambda **_: runtime())

    first_dir = tmp_path / "train-a"
    first = m2.train_m2_proxy_one_epoch(
        repository_root=repository,
        prepared_input_dir=prepared,
        output_dir=first_dir,
        checkpoint=checkpoint,
        audited_tokenizer_snapshot=snapshot,
        protocol=protocol,
        allow_experimental_mixed_supervision=True,
    )
    second_dir = tmp_path / "train-b"
    second = m2.train_m2_proxy_one_epoch(
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
    for name in m2._OUTPUT_FILES:
        assert (first_dir / name).read_bytes() == (second_dir / name).read_bytes()
        assert b"[HEADLESS]\n" not in (first_dir / name).read_bytes()
    manifest = m2.verify_m2_proxy_training(
        first_dir,
        prepared_input_dir=prepared,
        checkpoint=checkpoint,
        audited_tokenizer_snapshot=snapshot,
        protocol=protocol,
    )
    assert manifest.swap_invariance.maximum_equivalence_logit_difference <= 1e-7
    assert manifest.swap_invariance.relation_head_enabled is False
    assert manifest.contains_private_source_content is True
    assert manifest.redistribution_allowed is False

    forged_dir = tmp_path / "train-forged-predictions"
    shutil.copytree(first_dir, forged_dir)
    forged_predictions: list[m2.M2ProxyPrediction] = []
    for raw in (forged_dir / "predictions.jsonl").read_text().splitlines():
        prediction = m2.M2ProxyPrediction.model_validate_json(raw)
        forged_logit = -prediction.same_claim_logit
        forged_predictions.append(
            prediction.model_copy(
                update={
                    "same_claim_logit": forged_logit,
                    "same_claim_probability": m2._sigmoid(forged_logit),
                }
            )
        )
    (forged_dir / "predictions.jsonl").write_bytes(
        b"".join(
            canonical_json_bytes(item.model_dump(mode="json")) + b"\n"
            for item in forged_predictions
        )
    )
    forged_metrics = m0.M0ProxyTrainingMetrics.model_validate_json(
        (forged_dir / "metrics.json").read_text()
    )
    forged_by_split = {
        split: tuple(item for item in forged_predictions if item.split == split)
        for split in m2._SPLITS
    }
    forged_metrics = forged_metrics.model_copy(
        update={
            "diagnostics": {
                split: m2._m2_metric_set(forged_by_split[split]) for split in m2._SPLITS
            }
        }
    )
    (forged_dir / "metrics.json").write_bytes(
        canonical_json_bytes(forged_metrics.model_dump(mode="json")) + b"\n"
    )
    forged_manifest = json.loads((forged_dir / "manifest.json").read_text())
    for name in forged_manifest["output_sha256"]:
        forged_manifest["output_sha256"][name] = hashlib.sha256(
            (forged_dir / name).read_bytes()
        ).hexdigest()
    manifest_without_id = {
        key: value for key, value in forged_manifest.items() if key != "artifact_id"
    }
    forged_manifest["artifact_id"] = "experimental-m2-proxy-training:" + hash_canonical(
        manifest_without_id
    )
    (forged_dir / "manifest.json").write_bytes(canonical_json_bytes(forged_manifest) + b"\n")
    with pytest.raises(m2.ExperimentalM2ProxyError, match="do not replay"):
        m2.verify_m2_proxy_training(
            forged_dir,
            prepared_input_dir=prepared,
            checkpoint=checkpoint,
            audited_tokenizer_snapshot=snapshot,
            protocol=protocol,
        )

    swap = json.loads((second_dir / "swap_invariance.json").read_text())
    swap["maximum_equivalence_logit_difference"] = 1.0
    (second_dir / "swap_invariance.json").write_bytes(canonical_json_bytes(swap) + b"\n")
    with pytest.raises(m2.ExperimentalM2ProxyError, match="hash differs"):
        m2.verify_m2_proxy_training(second_dir)


def test_m2_verifier_rejects_output_and_member_symlinks(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    for name in m2._OUTPUT_FILES:
        (real / name).write_bytes(b"not reached")

    direct = tmp_path / "direct-link"
    direct.symlink_to(real, target_is_directory=True)
    with pytest.raises(m2.ExperimentalM2ProxyError, match="symlink"):
        m2.verify_m2_proxy_training(direct)

    actual_parent = tmp_path / "actual-parent"
    actual_parent.mkdir()
    nested = actual_parent / "nested"
    shutil.copytree(real, nested)
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(actual_parent, target_is_directory=True)
    with pytest.raises(m2.ExperimentalM2ProxyError, match="symlink"):
        m2.verify_m2_proxy_training(linked_parent / "nested")

    member_root = tmp_path / "member-root"
    shutil.copytree(real, member_root)
    external = tmp_path / "external-manifest.json"
    external.write_bytes(b"{}")
    (member_root / "manifest.json").unlink()
    (member_root / "manifest.json").symlink_to(external)
    with pytest.raises(m2.ExperimentalM2ProxyError, match="regular file"):
        m2.verify_m2_proxy_training(member_root)


def test_m2_cli_is_stable_and_training_fails_closed_without_opt_in(tmp_path: Path) -> None:
    runner = CliRunner()
    for command in ("train-m2-proxy", "verify-m2-training"):
        help_result = runner.invoke(app, [command, "--help"])
        assert help_result.exit_code == 0, help_result.output

    result = runner.invoke(
        app,
        [
            "train-m2-proxy",
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
    assert "M2 proxy training rejected" in result.output


def test_m2_config_is_canonical_yaml() -> None:
    loaded = m2.load_experimental_m2_proxy_config(
        Path("configs/models/experimental_m2_bidirectional_matcher_proxy_v1.yaml")
    )
    assert loaded.config.profile_id == "experimental_m2_bidirectional_matcher_proxy_v1"
    assert loaded.config_hash == hash_canonical(loaded.config.model_dump(mode="json"))
