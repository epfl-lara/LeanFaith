from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from typer.testing import CliRunner

from leanfaith.cli.app import app
from leanfaith.config.hashing import canonical_json_bytes, hash_file
from leanfaith.datasets.experimental_mixed_supervision import (
    ExperimentalHeadlessStatementView,
    ExperimentalMixedSupervisionManifest,
    ExperimentalMixedSupervisionRecord,
)
from leanfaith.models import m0_dual_encoder as m0
from leanfaith.models.tokenizer_audit import (
    CandidateSummary,
    FilePin,
    SnapshotBinding,
    TokenizerAuditManifest,
    TokenizerAuditSummary,
)
from leanfaith.schemas.manifest import CodeState


class _CharacterTokenizer:
    def encode(self, text: str, *, add_special_tokens: bool) -> list[int]:
        values = [ord(character) for character in text]
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
        ids = [row + [0] * (width - len(row)) for row in rows]
        masks = [[1] * len(row) + [0] * (width - len(row)) for row in rows]
        return {
            "input_ids": torch.tensor(ids, dtype=torch.long),
            "attention_mask": torch.tensor(masks, dtype=torch.long),
        }


def _protocol() -> m0.ExperimentalM0ProxyProtocolConfig:
    return m0.load_experimental_m0_proxy_config(
        Path("configs/models/experimental_m0_dual_encoder_proxy_v1.yaml")
    ).config


def _view(text: str, digit: str) -> ExperimentalHeadlessStatementView:
    return ExperimentalHeadlessStatementView.model_construct(
        headless=text,
        context_id="ctx:test",
        headless_sha256=digit * 64,
        origin_record_ids=(f"origin:{digit}",),
    )


def _record(
    index: int,
    *,
    split: m0.ProxySplit,
    target: m0.ProxyTarget,
    component_digit: str,
    text_suffix: str = "",
) -> ExperimentalMixedSupervisionRecord:
    component_id = component_digit if len(component_digit) == 64 else component_digit * 64
    return ExperimentalMixedSupervisionRecord.model_construct(
        record_id=f"experimental_mixed_pair:{index:064x}",
        split_component_id=f"split-component:{component_id}",
        split=split,
        pseudo_target=target,
        source=_view(f"(n : Nat) : n = n{text_suffix}", f"{index:x}"[-1]),
        candidate=_view(f"(m : Nat) : m = m{text_suffix}", f"{index + 1:x}"[-1]),
        private_source_content=index % 2 == 0,
        redistribution_allowed=index % 2 != 0,
        external_transmission_allowed=index % 2 != 0,
        release_eligible=index % 2 != 0,
        experimental_training_eligible=True,
        scientific_training_eligible=False,
        model_input_profile="headless_only_v1",
    )


def _records() -> tuple[ExperimentalMixedSupervisionRecord, ...]:
    return (
        _record(1, split="train", target="same_claim", component_digit="1"),
        _record(2, split="train", target="not_same_claim", component_digit="1"),
        _record(3, split="train", target="same_claim", component_digit="2"),
        _record(4, split="train", target="not_same_claim", component_digit="3"),
        _record(5, split="validation", target="same_claim", component_digit="4"),
        _record(6, split="test", target="not_same_claim", component_digit="5"),
    )


def _examples() -> tuple[m0.M0ProxyExample, ...]:
    return m0._make_examples(_records(), tokenizer=_CharacterTokenizer(), selected_length=512)


def _clean_code() -> CodeState:
    return CodeState(
        git_revision="1" * 40,
        git_dirty=False,
        base_git_commit="1" * 40,
        code_tree_hash="2" * 64,
        tracked_diff_hash="3" * 64,
        untracked_files=(),
    )


def _tokenizer_file_pins() -> dict[str, FilePin]:
    return {
        "config.json": FilePin(sha256="1" * 64, byte_count=1),
        "special_tokens_map.json": FilePin(sha256="2" * 64, byte_count=1),
        "tokenizer.json": FilePin(sha256="3" * 64, byte_count=1),
        "tokenizer_config.json": FilePin(sha256="4" * 64, byte_count=1),
    }


def _tokenizer_snapshot_hash() -> str:
    return m0.hash_canonical(
        {name: pin.model_dump(mode="json") for name, pin in sorted(_tokenizer_file_pins().items())}
    )


def _audit(
    tmp_path: Path,
    *,
    eligible: bool = True,
) -> tuple[TokenizerAuditManifest, TokenizerAuditSummary]:
    protocol = _protocol()
    candidate = CandidateSummary.model_construct(
        candidate="modernbert_base",
        model_id=protocol.backbone.model_id,
        revision=protocol.backbone.revision,
        eligible_at_selected_length=eligible,
    )
    snapshot = SnapshotBinding.model_construct(
        model_id=protocol.backbone.model_id,
        revision=protocol.backbone.revision,
        path=str(tmp_path),
        use_fast=True,
        native_max_length=8192,
        files=_tokenizer_file_pins(),
        snapshot_content_hash=_tokenizer_snapshot_hash(),
    )
    audit_id = f"tokenizer_audit:{'5' * 64}"
    summary = TokenizerAuditSummary.model_construct(
        audit_id=audit_id,
        profile_id=protocol.tokenizer_audit_profile_id,
        scientific_winner_selected=False,
        record_count=10_000,
        per_source={"mathlib": 5000, "sft_classic": 5000},
        selected_length=512,
        selection_reason="fixture",
        candidate_summaries=(candidate,),
        eligible_candidates=("modernbert_base",) if eligible else (),
        long_input_counts={"modernbert_base": 0},
    )
    manifest = TokenizerAuditManifest.model_construct(
        audit_id=audit_id,
        profile_id=protocol.tokenizer_audit_profile_id,
        selected_length=512,
        scientific_winner_selected=False,
        snapshots={"modernbert_base": snapshot},
    )
    return manifest, summary


def _run_binding(tmp_path: Path) -> m0.M0ProxyRunBinding:
    return m0.M0ProxyRunBinding(
        corpus_dir=str(tmp_path / "corpus"),
        dataset_id=f"experimental_mixed_supervision:{'6' * 64}",
        corpus_manifest_sha256="7" * 64,
        tokenizer_audit_dir=str(tmp_path / "audit"),
        tokenizer_audit_id=f"tokenizer_audit:{'5' * 64}",
        tokenizer_audit_manifest_sha256="8" * 64,
        tokenizer_audit_summary_sha256="9" * 64,
    )


def _fixture_receipt(
    protocol: m0.ExperimentalM0ProxyProtocolConfig,
    *,
    tokenizer_snapshot_hash: str,
) -> m0.M0OfficialCheckpointReceipt:
    required = {
        name: m0.M0CheckpointFile(sha256=pin.sha256, byte_count=pin.byte_count)
        for name, pin in _tokenizer_file_pins().items()
    }
    required.update(
        {
            "model.safetensors": m0.M0CheckpointFile(
                sha256=protocol.backbone.weight_sha256,
                byte_count=protocol.backbone.weight_byte_count,
            ),
        }
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
        "tokenizer_snapshot_content_hash": tokenizer_snapshot_hash,
        "local_files_only": True,
        "trust_remote_code": False,
    }
    return m0.M0OfficialCheckpointReceipt.model_validate(
        {**data, "receipt_id": "m0-official-checkpoint:" + m0.hash_canonical(data)}
    )


def test_committed_protocol_is_proxy_only_and_pins_modernbert_base() -> None:
    protocol = _protocol()

    assert protocol.protocol_status == "frozen_pending_exact_run_bindings"
    assert protocol.backbone.model_id == "answerdotai/ModernBERT-base"
    assert protocol.backbone.revision == "8949b909ec900327062f0ebf497f51aef5e6f0c8"
    assert protocol.architecture.input_profile == "headless_only_v1"
    assert protocol.architecture.symmetric_features == (
        "cosine",
        "absolute_difference",
        "product",
    )
    assert protocol.training.positive_fraction_per_batch == 0.5
    assert protocol.training.max_unique_variants_per_ancestry == 4
    assert protocol.training.microbatch_size == 4
    assert protocol.training.gradient_accumulation_steps == 8
    assert protocol.semantic_prediction is False
    assert protocol.scientific_training_eligible is False
    assert protocol.model_selection_eligible is False
    assert protocol.calibration_eligible is False
    assert protocol.evaluation_eligible is False
    assert protocol.release_claim_eligible is False


def test_examples_use_only_tagged_headless_and_preserve_long_input() -> None:
    records = (
        _record(1, split="train", target="same_claim", component_digit="1"),
        _record(
            2,
            split="train",
            target="not_same_claim",
            component_digit="2",
            text_suffix="x" * 600,
        ),
    )
    examples = m0._make_examples(records, tokenizer=_CharacterTokenizer(), selected_length=512)

    assert examples[0].source_text.startswith("[HEADLESS]\n")
    assert "SIGNATURE_EXPLICIT" not in examples[0].source_text
    assert examples[0].proxy_training_eligible is True
    assert examples[1].long_input is True
    assert examples[1].proxy_training_eligible is False
    assert examples[1].split == "train"


def test_component_crossing_splits_fails_before_training() -> None:
    records = (
        _record(1, split="train", target="same_claim", component_digit="1"),
        _record(2, split="test", target="not_same_claim", component_digit="1"),
    )

    with pytest.raises(m0.ExperimentalM0ProxyError, match="crosses splits"):
        m0._make_examples(records, tokenizer=_CharacterTokenizer(), selected_length=512)


def test_ancestry_weights_equalize_components_and_balanced_batches_do_not_oversample() -> None:
    train = tuple(item for item in _examples() if item.proxy_training_eligible)
    weights = m0.ancestry_normalized_proxy_weights(train)
    totals: dict[str, float] = {}
    for item, weight in zip(train, weights, strict=True):
        totals[item.split_component_id] = totals.get(item.split_component_id, 0.0) + weight

    assert {round(value, 12) for value in totals.values()} == {1.0}
    batches = m0.balanced_proxy_batches(train, batch_size=2, seed=1729)
    flattened = [item.record_id for batch in batches for item in batch]
    assert len(flattened) == len(set(flattened))
    assert all(
        {item.pseudo_target for item in batch} == {"same_claim", "not_same_claim"}
        for batch in batches
    )

    validation = next(item for item in _examples() if item.split == "validation")
    with pytest.raises(m0.ExperimentalM0ProxyError, match="train records only"):
        m0.ancestry_normalized_proxy_weights((*train, validation))


def test_epoch_schedule_caps_balances_and_accounts_for_every_eligible_record() -> None:
    records = tuple(
        _record(
            index,
            split="train",
            target="same_claim" if index <= 7 else "not_same_claim",
            component_digit="1" if index <= 7 else f"{index:x}"[-1],
        )
        for index in range(1, 21)
    )
    examples = m0._make_examples(records, tokenizer=_CharacterTokenizer(), selected_length=512)

    forward = m0.build_m0_epoch_schedule(examples, batch_size=4, seed=1729)
    reverse = m0.build_m0_epoch_schedule(tuple(reversed(examples)), batch_size=4, seed=1729)

    assert forward == reverse
    assert forward.selected_count + forward.omitted_count == len(examples)
    assert (
        forward.selected_counts_by_target["same_claim"]
        == forward.selected_counts_by_target["not_same_claim"]
    )
    assert forward.omission_counts_by_reason["ancestry_cap"] == 4
    selected = [item for item in forward.records if item.selection_status == "selected"]
    by_component: dict[str, list[m0.M0EpochSelectionRecord]] = {}
    for item in selected:
        by_component.setdefault(item.split_component_id, []).append(item)
    assert max(map(len, by_component.values())) <= 4
    assert all(
        sum(cast(float, item.loss_weight) for item in items) == pytest.approx(1.0)
        for items in by_component.values()
    )
    assert forward.selected_component_count == len(by_component)
    assert forward.loss_normalization_weight == pytest.approx(
        len(by_component) / forward.batch_count
    )
    assert {item.record_id for item in selected}.isdisjoint(
        {item.record_id for item in forward.records if item.selection_status == "omitted"}
    )


def test_tokenizer_decision_fails_closed_until_modernbert_is_eligible(tmp_path: Path) -> None:
    protocol = _protocol()
    audit, summary = _audit(tmp_path, eligible=False)

    with pytest.raises(m0.ExperimentalM0ProxyError, match="not eligible"):
        m0._tokenizer_decision(
            protocol=protocol,
            run_binding=_run_binding(tmp_path),
            audit_manifest=audit,
            audit_summary=summary,
        )


def test_input_artifact_is_content_addressed_exactly_replayable_and_verifiable(
    tmp_path: Path,
) -> None:
    protocol = _protocol()
    run_binding = _run_binding(tmp_path)
    audit, audit_summary = _audit(tmp_path)
    records = _records()
    corpus_manifest = ExperimentalMixedSupervisionManifest.model_construct(
        dataset_id=run_binding.dataset_id,
        record_count=len(records),
        model_input_profile="headless_only_v1",
    )
    input_dir = tmp_path / "inputs"
    input_dir.mkdir()
    inputs: dict[str, m0.M0ProxyInputBinding] = {}
    for index, name in enumerate(
        (
            "backbone_registry",
            "corpus_manifest",
            "corpus_records",
            "tokenizer_audit_manifest",
            "tokenizer_audit_summary",
        ),
        start=1,
    ):
        path = input_dir / name
        path.write_bytes(f"input-{index}".encode())
        inputs[name] = m0.M0ProxyInputBinding(
            path=str(path), sha256=hash_file(path), byte_count=path.stat().st_size
        )
    run_binding = run_binding.model_copy(
        update={
            "corpus_manifest_sha256": inputs["corpus_manifest"].sha256,
            "tokenizer_audit_manifest_sha256": inputs["tokenizer_audit_manifest"].sha256,
            "tokenizer_audit_summary_sha256": inputs["tokenizer_audit_summary"].sha256,
        }
    )
    protocol = protocol.model_copy(
        update={"backbone_registry_sha256": inputs["backbone_registry"].sha256}
    )
    payloads, summary = m0._build_payloads(
        repository_root=tmp_path,
        protocol=protocol,
        run_binding=run_binding,
        corpus_manifest=corpus_manifest,
        audit_manifest=audit,
        audit_summary=audit_summary,
        records=records,
        tokenizer=_CharacterTokenizer(),
        code=_clean_code(),
        inputs=inputs,
    )
    relocated_dir = tmp_path / "relocated-inputs"
    relocated_dir.mkdir()
    relocated_inputs: dict[str, m0.M0ProxyInputBinding] = {}
    for name, binding in inputs.items():
        relocated = relocated_dir / name
        relocated.write_bytes(Path(binding.path).read_bytes())
        relocated_inputs[name] = m0.M0ProxyInputBinding(
            path=str(relocated),
            sha256=binding.sha256,
            byte_count=binding.byte_count,
        )
    relocated_binding = run_binding.model_copy(
        update={
            "corpus_dir": str(tmp_path / "elsewhere-corpus"),
            "tokenizer_audit_dir": str(tmp_path / "elsewhere-audit"),
        }
    )
    relocated_payloads, _ = m0._build_payloads(
        repository_root=tmp_path / "different-worktree",
        protocol=protocol,
        run_binding=relocated_binding,
        corpus_manifest=corpus_manifest,
        audit_manifest=audit,
        audit_summary=audit_summary,
        records=records,
        tokenizer=_CharacterTokenizer(),
        code=_clean_code(),
        inputs=relocated_inputs,
    )
    assert relocated_payloads == payloads
    output = tmp_path / "output"

    assert m0._write_or_replay(output, payloads) is False
    assert m0._write_or_replay(output, payloads) is True
    manifest = m0.verify_experimental_m0_proxy_inputs(output)
    assert manifest.record_count == len(records)
    assert manifest.training_record_count == summary.training_record_count
    assert manifest.model_weights_loaded is False
    assert manifest.training_executed is False

    summary_path = output / "summary.json"
    value = json.loads(summary_path.read_text())
    value["record_count"] += 1
    summary_path.write_bytes(canonical_json_bytes(value) + b"\n")
    with pytest.raises(m0.ExperimentalM0ProxyError, match="hash differs"):
        m0.verify_experimental_m0_proxy_inputs(output)


def test_checkpoint_binding_refuses_tokenizer_only_snapshot_and_detects_drift(
    tmp_path: Path,
) -> None:
    protocol = _protocol()
    revision = protocol.backbone.revision
    snapshot = tmp_path / revision
    snapshot.mkdir()
    files = {
        "config.json": b'{"model_type":"modernbert"}\n',
        "special_tokens_map.json": b"{}\n",
        "tokenizer.json": b"{}\n",
        "tokenizer_config.json": b"{}\n",
        "model.safetensors": b"fixture weights",
    }
    for name, payload in files.items():
        (snapshot / name).write_bytes(payload)
    audit_files = {
        name: FilePin(sha256=hash_file(snapshot / name), byte_count=len(payload))
        for name, payload in files.items()
        if name != "model.safetensors"
    }
    audit = SnapshotBinding(
        model_id=protocol.backbone.model_id,
        revision=revision,
        path=str(snapshot),
        use_fast=True,
        native_max_length=8192,
        files=audit_files,
        snapshot_content_hash=m0.hash_canonical(
            {name: pin.model_dump(mode="json") for name, pin in sorted(audit_files.items())}
        ),
    )

    with pytest.raises(m0.ExperimentalM0ProxyError, match="official receipt"):
        m0.bind_local_modernbert_checkpoint(
            snapshot, protocol=protocol, audited_tokenizer_snapshot=audit
        )

    required = {
        name: m0.M0CheckpointFile(sha256=hash_file(snapshot / name), byte_count=len(payload))
        for name, payload in files.items()
    }
    receipt_data: dict[str, Any] = {
        "schema_version": 1,
        "model_id": protocol.backbone.model_id,
        "revision": revision,
        "hf_snapshot_api_url": protocol.backbone.hf_snapshot_api_url,
        "receipt_basis": protocol.backbone.receipt_basis,
        "required_files": {
            name: pin.model_dump(mode="json") for name, pin in sorted(required.items())
        },
        "tokenizer_snapshot_content_hash": audit.snapshot_content_hash,
        "local_files_only": True,
        "trust_remote_code": False,
    }
    receipt = m0.M0OfficialCheckpointReceipt.model_validate(
        {
            **receipt_data,
            "receipt_id": "m0-official-checkpoint:" + m0.hash_canonical(receipt_data),
        }
    )
    binding = m0._bind_checkpoint_against_receipt(snapshot, receipt=receipt)
    assert m0.verify_local_modernbert_checkpoint(binding) == snapshot
    (snapshot / "model.safetensors").write_bytes(b"changed weights")
    with pytest.raises(m0.ExperimentalM0ProxyError, match="differs from official receipt"):
        m0.verify_local_modernbert_checkpoint(binding)


def test_real_pinned_modernbert_snapshot_matches_official_receipt_when_available() -> None:
    snapshot = Path(
        "/storage/milikic/models/hub/models--answerdotai--ModernBERT-base/snapshots/"
        "8949b909ec900327062f0ebf497f51aef5e6f0c8"
    )
    if not snapshot.exists():
        pytest.skip("pinned ModernBERT snapshot is not installed")
    protocol = _protocol()
    tokenizer_files = {
        name: FilePin(
            sha256=hash_file(snapshot / name), byte_count=(snapshot / name).stat().st_size
        )
        for name in (
            "config.json",
            "special_tokens_map.json",
            "tokenizer.json",
            "tokenizer_config.json",
        )
    }
    audited = SnapshotBinding(
        model_id=protocol.backbone.model_id,
        revision=protocol.backbone.revision,
        path=str(snapshot),
        use_fast=True,
        native_max_length=8192,
        files=tokenizer_files,
        snapshot_content_hash=m0.hash_canonical(
            {name: pin.model_dump(mode="json") for name, pin in sorted(tokenizer_files.items())}
        ),
    )

    binding = m0.bind_local_modernbert_checkpoint(
        snapshot, protocol=protocol, audited_tokenizer_snapshot=audited
    )

    assert binding.receipt.required_files["model.safetensors"].sha256 == (
        "340ac08b74eef0d7bdec2d7981a6a3d4249bf0e6aab60634b72ad02c2b8023a9"
    )
    assert m0.verify_local_modernbert_checkpoint(binding) == snapshot.resolve()


def test_real_m0_module_shares_encoder_normalizes_and_is_exactly_symmetric() -> None:
    torch = pytest.importorskip("torch")

    class TinyEncoder(torch.nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.embedding = torch.nn.Embedding(32, 4)
            self.calls = 0

        def forward(self, *, input_ids: Any, attention_mask: Any) -> object:
            del attention_mask
            self.calls += 1
            return SimpleNamespace(last_hidden_state=self.embedding(input_ids))

    torch.manual_seed(7)
    encoder = TinyEncoder()
    model = m0.build_m0_dual_encoder_module(encoder=encoder, hidden_size=4)
    model.eval()
    source_ids = torch.tensor([[1, 2, 3], [4, 5, 0]])
    source_mask = torch.tensor([[1, 1, 1], [1, 1, 0]])
    candidate_ids = torch.tensor([[3, 2, 1], [6, 7, 8]])
    candidate_mask = torch.tensor([[1, 1, 1], [1, 1, 1]])

    forward = model(
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
    assert encoder.calls == 4
    assert torch.allclose(forward["probabilities"], swapped["probabilities"], atol=0, rtol=0)
    assert torch.allclose(
        forward["symmetric_features"], swapped["symmetric_features"], atol=0, rtol=0
    )
    assert torch.allclose(
        torch.linalg.vector_norm(forward["source_embeddings"], dim=-1),
        torch.ones(2),
    )
    assert torch.allclose(
        torch.linalg.vector_norm(forward["candidate_embeddings"], dim=-1),
        torch.ones(2),
    )


def test_invalid_attention_mask_fails_closed() -> None:
    torch = pytest.importorskip("torch")

    class TinyEncoder(torch.nn.Module):
        def forward(self, *, input_ids: Any, attention_mask: Any) -> object:
            del attention_mask
            hidden = torch.ones((*input_ids.shape, 2))
            return SimpleNamespace(last_hidden_state=hidden)

    model = m0.build_m0_dual_encoder_module(encoder=TinyEncoder(), hidden_size=2)
    ids = torch.tensor([[1, 2]])
    empty = torch.tensor([[0, 0]])

    with pytest.raises(ValueError, match="empty sequence"):
        model(
            source_input_ids=ids,
            source_attention_mask=empty,
            candidate_input_ids=ids,
            candidate_attention_mask=empty,
        )


def test_weighted_bce_uses_static_ancestry_weights() -> None:
    torch = pytest.importorskip("torch")
    logits = torch.tensor([0.0, 2.0, -1.0])
    labels = torch.tensor([0.0, 1.0, 1.0])
    weights = torch.tensor([1.0, 0.5, 0.5])

    actual = cast(
        Any,
        m0._weighted_binary_cross_entropy(
            logits=logits, labels=labels, weights=weights, torch=torch
        ),
    )
    individual = torch.nn.functional.binary_cross_entropy_with_logits(
        logits, labels, reduction="none"
    )
    expected = (individual * weights).sum() / weights.sum()

    assert actual.item() == pytest.approx(expected.item())
    assert actual.item() != pytest.approx(individual.mean().item())

    fixed = cast(
        Any,
        m0._weighted_binary_cross_entropy(
            logits=logits,
            labels=labels,
            weights=weights,
            torch=torch,
            normalization_weight=4.0,
        ),
    )
    assert fixed.item() == pytest.approx(((individual * weights).sum() / 4.0).item())


def test_tiny_one_epoch_training_is_executable_private_safe_and_exactly_replayable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    torch = pytest.importorskip("torch")
    protocol = _protocol()
    run_binding = _run_binding(tmp_path)
    audit, audit_summary = _audit(tmp_path)
    records = (
        *(
            _record(
                index,
                split="train",
                target="same_claim" if index <= 32 else "not_same_claim",
                component_digit=f"{index:064x}",
                text_suffix="PRIVATE_SENTINEL" if index == 1 else "",
            )
            for index in range(1, 65)
        ),
        _record(65, split="validation", target="same_claim", component_digit=f"{65:064x}"),
        _record(66, split="validation", target="not_same_claim", component_digit=f"{66:064x}"),
        _record(67, split="test", target="same_claim", component_digit=f"{67:064x}"),
        _record(68, split="test", target="not_same_claim", component_digit=f"{68:064x}"),
    )
    corpus_manifest = ExperimentalMixedSupervisionManifest.model_construct(
        dataset_id=run_binding.dataset_id,
        record_count=len(records),
        model_input_profile="headless_only_v1",
    )
    input_dir = tmp_path / "input-pins"
    input_dir.mkdir()
    inputs: dict[str, m0.M0ProxyInputBinding] = {}
    for index, name in enumerate(
        (
            "backbone_registry",
            "corpus_manifest",
            "corpus_records",
            "tokenizer_audit_manifest",
            "tokenizer_audit_summary",
        ),
        start=1,
    ):
        path = input_dir / name
        path.write_bytes(f"pin-{index}".encode())
        inputs[name] = m0.M0ProxyInputBinding(
            path=str(path), sha256=hash_file(path), byte_count=path.stat().st_size
        )
    protocol = protocol.model_copy(
        update={"backbone_registry_sha256": inputs["backbone_registry"].sha256}
    )
    run_binding = run_binding.model_copy(
        update={
            "corpus_manifest_sha256": inputs["corpus_manifest"].sha256,
            "tokenizer_audit_manifest_sha256": inputs["tokenizer_audit_manifest"].sha256,
            "tokenizer_audit_summary_sha256": inputs["tokenizer_audit_summary"].sha256,
        }
    )
    payloads, _ = m0._build_payloads(
        repository_root=tmp_path,
        protocol=protocol,
        run_binding=run_binding,
        corpus_manifest=corpus_manifest,
        audit_manifest=audit,
        audit_summary=audit_summary,
        records=records,
        tokenizer=_CharacterTokenizer(),
        code=_clean_code(),
        inputs=inputs,
    )
    prepared = tmp_path / "prepared"
    assert m0._write_or_replay(prepared, payloads) is False

    class TinyEncoder(torch.nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.embedding = torch.nn.Embedding(128, 4)

        def forward(self, *, input_ids: Any, attention_mask: Any) -> object:
            del attention_mask
            return SimpleNamespace(last_hidden_state=self.embedding(input_ids))

    checkpoint_dir = tmp_path / protocol.backbone.revision
    checkpoint_dir.mkdir()
    receipt = _fixture_receipt(protocol, tokenizer_snapshot_hash=_tokenizer_snapshot_hash())
    checkpoint = m0.M0LocalCheckpointBinding(snapshot_path=str(checkpoint_dir), receipt=receipt)

    def runtime() -> m0.LoadedM0ProxyRuntime:
        torch.manual_seed(101)
        model = m0.build_m0_dual_encoder_module(encoder=TinyEncoder(), hidden_size=4)
        tokenizer = _CharacterTokenizer()
        return m0.LoadedM0ProxyRuntime(
            model=model,
            tokenizer=tokenizer,
            selected_length=512,
            checkpoint=checkpoint,
            audited_tokenizer_snapshot=audit.snapshots["modernbert_base"],
            initial_model_state_sha256=hashlib.sha256(
                m0._safetensors_state_bytes(model, module_importer=importlib.import_module)
            ).hexdigest(),
            prepared_tokenization_sha256=m0._prepared_tokenization_sha256(
                tokenizer, m0._load_prepared_examples(prepared)
            ),
        )

    repository = tmp_path / "repository"
    repository.mkdir()
    monkeypatch.setattr(m0, "collect_code_state", lambda _: _clean_code())
    monkeypatch.setattr(m0, "verify_local_modernbert_checkpoint", lambda _: checkpoint_dir)
    monkeypatch.setattr(m0, "_verify_audited_tokenizer_snapshot", lambda _: tmp_path)
    monkeypatch.setattr(m0, "_load_audited_tokenizer", lambda _: _CharacterTokenizer())
    monkeypatch.setattr(m0, "load_m0_proxy_runtime", lambda **_: runtime())

    first_dir = tmp_path / "train-a"
    first = m0.train_m0_proxy_one_epoch(
        repository_root=repository,
        prepared_input_dir=prepared,
        output_dir=first_dir,
        checkpoint=checkpoint,
        audited_tokenizer_snapshot=audit.snapshots["modernbert_base"],
        allow_experimental_mixed_supervision=True,
    )
    second_dir = tmp_path / "train-b"
    second = m0.train_m0_proxy_one_epoch(
        repository_root=repository,
        prepared_input_dir=prepared,
        output_dir=second_dir,
        checkpoint=checkpoint,
        audited_tokenizer_snapshot=audit.snapshots["modernbert_base"],
        allow_experimental_mixed_supervision=True,
    )

    assert first.artifact_id == second.artifact_id
    assert first.optimizer_steps == 2
    assert first.examples_exposed == 64
    assert m0.verify_m0_proxy_training(first_dir).training_executed is True
    for name in m0._TRAINING_OUTPUT_FILES:
        assert (first_dir / name).read_bytes() == (second_dir / name).read_bytes()
        assert b"PRIVATE_SENTINEL" not in (first_dir / name).read_bytes()
    manifest = m0.M0ProxyTrainingManifest.model_validate(
        json.loads((first_dir / "manifest.json").read_text())
    )
    assert manifest.contains_private_source_content is True
    assert manifest.redistribution_allowed is False
    assert manifest.external_transmission_allowed is False
    assert manifest.release_eligible is False
    assert manifest.effective_batch_size == 32
    assert manifest.microbatch_size == 4
    assert manifest.gradient_accumulation_steps == 8


def test_m0_cli_commands_have_stable_help_and_fail_closed_without_opt_in(tmp_path: Path) -> None:
    runner = CliRunner()
    for command in (
        "prepare-m0-proxy",
        "verify-m0-proxy",
        "train-m0-proxy",
        "verify-m0-training",
    ):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0, result.output

    prepare = runner.invoke(
        app,
        [
            "prepare-m0-proxy",
            "--corpus-dir",
            str(tmp_path / "corpus"),
            "--tokenizer-audit-dir",
            str(tmp_path / "audit"),
            "--output-dir",
            str(tmp_path / "prepared"),
        ],
    )
    assert prepare.exit_code == 1
    assert "preparation rejected" in prepare.output

    train = runner.invoke(
        app,
        [
            "train-m0-proxy",
            "--prepared-input-dir",
            str(tmp_path / "prepared"),
            "--tokenizer-audit-dir",
            str(tmp_path / "audit"),
            "--checkpoint-dir",
            str(tmp_path / "checkpoint"),
            "--output-dir",
            str(tmp_path / "training"),
        ],
    )
    assert train.exit_code == 1
    assert "training rejected" in train.output
