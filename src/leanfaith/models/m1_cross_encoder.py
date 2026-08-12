"""Executable packed cross-encoder over the frozen mixed-proxy corpus.

This module is an engineering/proxy milestone, not a scientific LeanFaith
model.  It consumes the exact content-addressed inputs prepared for M0 because
that artifact already contains the only model-visible view currently admitted
by the mixed corpus: ``[HEADLESS]``.  M1 changes only the model interaction:
reference and candidate are tagged, packed into one sequence, encoded jointly,
and classified from one masked-mean representation.

All labels remain machine proxy labels.  Every artifact therefore rejects
semantic, calibration, model-selection, evaluation, gate-credit, and release
claims.  The standalone verifier checks every frozen output byte and can also
bind the result back to exact code, prepared inputs, tokenizer, and pretrained
checkpoint.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import math
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import Field, field_validator, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.loading import LoadedConfig, load_config
from leanfaith.config.models import StrictModel
from leanfaith.models.m0_dual_encoder import (
    ExperimentalM0ProxyBoundary,
    ExperimentalM0ProxyError,
    M0EpochSchedule,
    M0LocalCheckpointBinding,
    M0OfficialCheckpointReceipt,
    M0ProxyBackboneProtocol,
    M0ProxyExample,
    M0ProxyPrediction,
    M0ProxyTrainingMetrics,
    M0ProxyTrainingProtocol,
    M0TrainingRuntimeVersions,
    ModuleImporter,
    _canonical_jsonl,
    _load_audited_tokenizer,
    _load_prepared_examples,
    _metric_set,
    _paths_overlap,
    _real_directory,
    _regular_file,
    _reject_symlinks,
    _runtime_versions,
    _safetensors_state_bytes,
    _strict_json,
    _verify_audited_tokenizer_snapshot,
    _verify_clean_code,
    _weighted_binary_cross_entropy,
    _write_or_replay_training,
    build_m0_epoch_schedule,
    verify_experimental_m0_proxy_inputs,
    verify_local_modernbert_checkpoint,
)
from leanfaith.models.tokenizer_audit import SnapshotBinding
from leanfaith.schemas.manifest import CodeState, collect_code_state

_HEX64 = r"^[0-9a-f]{64}$"
_ARTIFACT_ID = r"^experimental-m1-proxy-training:[0-9a-f]{64}$"
_M0_INPUT_ID = r"^experimental-m0-proxy-inputs:[0-9a-f]{64}$"
_DATASET_ID = r"^experimental_mixed_supervision:[0-9a-f]{64}$"
_AUDIT_ID = r"^tokenizer_audit:[0-9a-f]{64}$"
_OUTPUT_FILES = frozenset(
    {
        "epoch_schedule.json",
        "manifest.json",
        "metrics.json",
        "model.safetensors",
        "predictions.jsonl",
    }
)
_NON_MANIFEST_OUTPUTS = _OUTPUT_FILES - {"manifest.json"}
_REFERENCE_TAG = "[REFERENCE]\n"
_CANDIDATE_TAG = "\n[CANDIDATE]\n"
_SPLITS = ("test", "train", "validation")


class ExperimentalM1ProxyError(ExperimentalM0ProxyError):
    """An M1 proxy prerequisite, policy, or replay invariant failed."""


class M1ProxyArchitectureProtocol(StrictModel):
    architecture: Literal["packed_cross_encoder_mean_pool_v1"] = "packed_cross_encoder_mean_pool_v1"
    input_profile: Literal["tagged_headless_pair_v1"] = "tagged_headless_pair_v1"
    reference_tag: Literal["[REFERENCE]"] = "[REFERENCE]"
    candidate_tag: Literal["[CANDIDATE]"] = "[CANDIDATE]"
    packing_order: Literal["reference_then_candidate"] = "reference_then_candidate"
    pooling: Literal["attention_masked_mean"] = "attention_masked_mean"
    head: Literal["single_linear_logit"] = "single_linear_logit"
    encoder_instances: Literal[1] = 1
    encoder_calls_per_pair: Literal[1] = 1
    same_claim_only: Literal[True] = True
    ambiguity_head_enabled: Literal[False] = False
    relation_head_enabled: Literal[False] = False
    auxiliary_heads_enabled: Literal[False] = False


class ExperimentalM1ProxyProtocolConfig(ExperimentalM0ProxyBoundary):
    """Frozen proxy protocol; exact corpus inputs remain the verified M0 bundle."""

    schema_version: Literal[1] = 1
    profile_id: str = Field(min_length=1)
    protocol_status: Literal["frozen_proxy_diagnostic"] = "frozen_proxy_diagnostic"
    required_opt_in_flag: Literal["--allow-experimental-mixed-supervision"] = (
        "--allow-experimental-mixed-supervision"
    )
    tokenizer_audit_profile_id: str = Field(min_length=1)
    backbone_registry_path: str = Field(min_length=1)
    backbone_registry_sha256: str = Field(pattern=_HEX64)
    backbone: M0ProxyBackboneProtocol
    architecture: M1ProxyArchitectureProtocol
    training: M0ProxyTrainingProtocol

    @field_validator("backbone_registry_path")
    @classmethod
    def _registry_path_is_relative(cls, value: str) -> str:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("backbone_registry_path must be repository-relative")
        return value


def load_experimental_m1_proxy_config(
    path: Path,
) -> LoadedConfig[ExperimentalM1ProxyProtocolConfig]:
    """Load the packed cross-encoder proxy protocol."""

    return load_config(path, ExperimentalM1ProxyProtocolConfig)


class M1PackedTokenization(StrictModel):
    """Text-free accounting for the exact packed tokenizer decision."""

    schema_version: Literal[1] = 1
    record_count: int = Field(gt=0)
    eligible_training_count: int = Field(gt=0)
    long_input_count: int = Field(ge=0)
    long_input_counts_by_split: dict[str, int]
    tokenization_sha256: str = Field(pattern=_HEX64)

    @model_validator(mode="after")
    def _counts_reconcile(self) -> M1PackedTokenization:
        if tuple(self.long_input_counts_by_split) != _SPLITS:
            raise ValueError("M1 packed long-input split counts are not canonical")
        if sum(self.long_input_counts_by_split.values()) != self.long_input_count:
            raise ValueError("M1 packed long-input counts do not reconcile")
        if self.long_input_count > self.record_count:
            raise ValueError("M1 packed long-input count exceeds record count")
        return self


class M1ProxyTrainingManifest(ExperimentalM0ProxyBoundary):
    """Portable, content-addressed record of one packed-proxy epoch."""

    schema_version: Literal[1] = 1
    artifact_id: str = Field(pattern=_ARTIFACT_ID)
    artifact_kind: Literal["experimental_m1_proxy_training_v1"] = (
        "experimental_m1_proxy_training_v1"
    )
    prepared_input_artifact_id: str = Field(pattern=_M0_INPUT_ID)
    prepared_input_manifest_sha256: str = Field(pattern=_HEX64)
    dataset_id: str = Field(pattern=_DATASET_ID)
    protocol_hash: str = Field(pattern=_HEX64)
    protocol: ExperimentalM1ProxyProtocolConfig
    code: CodeState
    pretrained_checkpoint: M0OfficialCheckpointReceipt
    tokenizer_audit_id: str = Field(pattern=_AUDIT_ID)
    tokenizer_snapshot_content_hash: str = Field(pattern=_HEX64)
    packed_tokenization: M1PackedTokenization
    runtime: M0TrainingRuntimeVersions
    epoch_schedule_sha256: str = Field(pattern=_HEX64)
    selected_record_set_sha256: str = Field(pattern=_HEX64)
    optimizer: Literal["AdamW"] = "AdamW"
    learning_rate: float = Field(gt=0.0)
    weight_decay: float = Field(ge=0.0)
    seed: int = Field(ge=0)
    epoch_count: Literal[1] = 1
    optimizer_steps: int = Field(gt=0)
    examples_exposed: int = Field(gt=0)
    selected_component_count: int = Field(gt=0)
    loss_normalization_weight: float = Field(gt=0.0)
    effective_batch_size: Literal[32] = 32
    microbatch_size: Literal[4] = 4
    gradient_accumulation_steps: Literal[8] = 8
    initial_model_state_sha256: str = Field(pattern=_HEX64)
    contains_private_source_content: bool
    redistribution_allowed: bool
    external_transmission_allowed: bool
    release_eligible: bool
    output_sha256: dict[str, str]
    model_weights_loaded: Literal[True] = True
    training_executed: Literal[True] = True

    @model_validator(mode="after")
    def _manifest_is_coherent(self) -> M1ProxyTrainingManifest:
        if self.protocol_hash != hash_canonical(self.protocol.model_dump(mode="json")):
            raise ValueError("M1 protocol hash differs from embedded protocol")
        if self.code.git_dirty or self.code.code_tree_hash is None or self.code.untracked_files:
            raise ValueError("M1 proxy training requires clean fully tracked code")
        if set(self.output_sha256) != _NON_MANIFEST_OUTPUTS:
            raise ValueError("M1 training manifest output set is not exact")
        if self.contains_private_source_content and (
            self.redistribution_allowed
            or self.external_transmission_allowed
            or self.release_eligible
        ):
            raise ValueError("private-trained M1 artifact cannot be shared or released")
        if self.release_eligible and not self.redistribution_allowed:
            raise ValueError("M1 release policy is incoherent")
        if self.microbatch_size * self.gradient_accumulation_steps != self.effective_batch_size:
            raise ValueError("M1 training microbatch accounting differs")
        expected = "experimental-m1-proxy-training:" + hash_canonical(
            self.model_dump(mode="json", exclude={"artifact_id"})
        )
        if self.artifact_id != expected:
            raise ValueError("M1 training artifact ID differs from canonical content")
        return self


@dataclass(frozen=True, slots=True)
class M1ProxyTrainingArtifacts:
    output_dir: Path
    artifact_id: str
    optimizer_steps: int
    examples_exposed: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class LoadedM1ProxyRuntime:
    model: object
    tokenizer: object
    selected_length: Literal[512, 1024]
    checkpoint: M0LocalCheckpointBinding
    audited_tokenizer_snapshot: SnapshotBinding
    protocol: ExperimentalM1ProxyProtocolConfig
    initial_model_state_sha256: str
    packed_tokenization: M1PackedTokenization


def pack_m1_pair(example: M0ProxyExample) -> str:
    """Pack one ordered reference/candidate pair without adding untracked views."""

    return _REFERENCE_TAG + example.source_text + _CANDIDATE_TAG + example.candidate_text


def _validate_protocol_against_input(
    protocol: ExperimentalM1ProxyProtocolConfig,
    *,
    prepared_input_dir: Path,
) -> object:
    manifest = verify_experimental_m0_proxy_inputs(prepared_input_dir)
    if (
        protocol.tokenizer_audit_profile_id != manifest.protocol.tokenizer_audit_profile_id
        or protocol.backbone_registry_path != manifest.protocol.backbone_registry_path
        or protocol.backbone_registry_sha256 != manifest.protocol.backbone_registry_sha256
        or protocol.backbone != manifest.protocol.backbone
        or protocol.training != manifest.protocol.training
    ):
        raise ExperimentalM1ProxyError("M1 protocol differs from frozen M0 input bindings")
    return manifest


def build_m1_cross_encoder_module(
    *,
    encoder: object,
    hidden_size: int,
    module_importer: ModuleImporter = importlib.import_module,
) -> object:
    """Build one joint-encoding binary classifier without eager torch imports."""

    if hidden_size <= 0:
        raise ExperimentalM1ProxyError("M1 hidden_size must be positive")
    try:
        torch = cast(Any, module_importer("torch"))
    except (ImportError, ModuleNotFoundError) as exc:
        raise ExperimentalM1ProxyError("M1 requires the optional torch runtime") from exc
    if not isinstance(encoder, torch.nn.Module):
        raise ExperimentalM1ProxyError("M1 encoder must be a torch.nn.Module")

    class _M1CrossEncoder(torch.nn.Module):  # type: ignore[misc, name-defined]
        def __init__(self, joint_encoder: object, width: int) -> None:
            super().__init__()
            self.encoder = cast(Any, joint_encoder)
            self.hidden_size = width
            self.same_claim_head = torch.nn.Linear(width, 1)

        def forward(self, *, input_ids: Any, attention_mask: Any) -> dict[str, Any]:
            if input_ids.ndim != 2 or attention_mask.ndim != 2:
                raise ValueError("M1 input_ids and attention_mask must be rank-two")
            if input_ids.shape != attention_mask.shape:
                raise ValueError("M1 input_ids and attention_mask shapes differ")
            if bool((attention_mask.sum(dim=1) <= 0).any().item()):
                raise ValueError("M1 attention mask contains an empty sequence")
            output = self.encoder(input_ids=input_ids, attention_mask=attention_mask)
            hidden = getattr(output, "last_hidden_state", None)
            if hidden is None:
                try:
                    hidden = output[0]
                except (KeyError, IndexError, TypeError) as exc:
                    raise ValueError("M1 encoder output lacks last_hidden_state") from exc
            if hidden.ndim != 3 or hidden.shape[:2] != input_ids.shape:
                raise ValueError("M1 encoder hidden state has an incompatible shape")
            if hidden.shape[-1] != self.hidden_size:
                raise ValueError("M1 encoder hidden width differs from configured hidden_size")
            mask = attention_mask.to(dtype=hidden.dtype).unsqueeze(-1)
            pooled = (hidden * mask).sum(dim=1) / mask.sum(dim=1).clamp_min(1.0)
            logits = self.same_claim_head(pooled).squeeze(-1)
            return {
                "logits": logits,
                "probabilities": torch.sigmoid(logits),
                "pooled_pair_embeddings": pooled,
            }

    _M1CrossEncoder.__name__ = "M1CrossEncoder"
    return _M1CrossEncoder(encoder, hidden_size)


def _packed_tokenization(
    tokenizer: object,
    examples: Sequence[M0ProxyExample],
    *,
    selected_length: int,
) -> tuple[M1PackedTokenization, dict[str, int]]:
    if not examples:
        raise ExperimentalM1ProxyError("M1 tokenization binding requires examples")
    encode = getattr(tokenizer, "encode", None)
    if not callable(encode):
        raise ExperimentalM1ProxyError("M1 runtime tokenizer lacks encode")
    digest = hashlib.sha256()
    lengths: dict[str, int] = {}
    long_counts: Counter[str] = Counter()
    eligible_training = 0
    for item in sorted(examples, key=lambda example: example.record_id):
        try:
            token_ids = tuple(
                int(value) for value in encode(pack_m1_pair(item), add_special_tokens=True)
            )
        except Exception as exc:
            raise ExperimentalM1ProxyError(
                "M1 tokenizer binding failed without persisting private text"
            ) from exc
        if not token_ids:
            raise ExperimentalM1ProxyError("M1 tokenizer returned an empty pair")
        lengths[item.record_id] = len(token_ids)
        is_long = len(token_ids) > selected_length
        if is_long:
            long_counts[item.split] += 1
        elif item.split == "train" and item.proxy_training_eligible:
            eligible_training += 1
        digest.update(
            canonical_json_bytes({"record_id": item.record_id, "packed_token_ids": token_ids})
        )
        digest.update(b"\n")
    profile = M1PackedTokenization(
        record_count=len(examples),
        eligible_training_count=eligible_training,
        long_input_count=sum(long_counts.values()),
        long_input_counts_by_split={split: long_counts[split] for split in _SPLITS},
        tokenization_sha256=digest.hexdigest(),
    )
    return profile, lengths


def _m1_schedule_examples(
    examples: Sequence[M0ProxyExample],
    *,
    packed_lengths: Mapping[str, int],
    selected_length: int,
) -> tuple[M0ProxyExample, ...]:
    result: list[M0ProxyExample] = []
    for item in examples:
        if (
            item.split == "train"
            and item.proxy_training_eligible
            and packed_lengths[item.record_id] <= selected_length
        ):
            # This private scheduling view reuses the battle-tested ancestry
            # scheduler.  It is never serialized as an M0 example.
            result.append(item.model_copy(update={"long_input": False}))
    if not result:
        raise ExperimentalM1ProxyError("M1 has no packed training examples")
    return tuple(result)


def _tensorize_m1_batch(
    *,
    tokenizer: object,
    examples: Sequence[M0ProxyExample],
    selected_length: int,
    weights: Sequence[float],
    device: str,
    module_importer: ModuleImporter,
) -> dict[str, object]:
    if not examples or len(examples) != len(weights):
        raise ExperimentalM1ProxyError("M1 collator requires aligned examples and weights")
    if any(not math.isfinite(weight) or weight <= 0.0 for weight in weights):
        raise ExperimentalM1ProxyError("M1 collator weights must be finite and positive")
    encode = getattr(tokenizer, "encode", None)
    if not callable(encode):
        raise ExperimentalM1ProxyError("M1 collator tokenizer lacks encode")
    texts = [pack_m1_pair(item) for item in examples]
    if any(len(encode(text, add_special_tokens=True)) > selected_length for text in texts):
        raise ExperimentalM1ProxyError("M1 collator cannot silently truncate long pairs")
    try:
        torch = cast(Any, module_importer("torch"))
        tokenize = cast(Callable[..., Mapping[str, Any]], tokenizer)
        packed = tokenize(
            texts,
            padding=True,
            truncation=True,
            max_length=selected_length,
            return_tensors="pt",
        )
    except Exception as exc:
        raise ExperimentalM1ProxyError(
            "M1 tokenizer failed without persisting private text"
        ) from exc
    if not {"input_ids", "attention_mask"}.issubset(packed):
        raise ExperimentalM1ProxyError("M1 tokenizer output lacks IDs or attention masks")
    for value in (packed["input_ids"], packed["attention_mask"]):
        if value.ndim != 2 or value.shape[0] != len(examples):
            raise ExperimentalM1ProxyError("M1 tokenized batch has an incompatible shape")
        if value.shape[1] > selected_length:
            raise ExperimentalM1ProxyError("M1 tokenizer exceeded selected context length")
    if bool((packed["attention_mask"].sum(dim=1) <= 0).any().item()):
        raise ExperimentalM1ProxyError("M1 tokenizer produced an empty pair")
    return {
        "input_ids": packed["input_ids"].to(device),
        "attention_mask": packed["attention_mask"].to(device),
        "labels": torch.tensor(
            [1.0 if item.pseudo_target == "same_claim" else 0.0 for item in examples],
            dtype=torch.float32,
            device=device,
        ),
        "weights": torch.tensor(tuple(weights), dtype=torch.float32, device=device),
    }


def load_m1_proxy_runtime(
    *,
    prepared_input_dir: Path,
    checkpoint: M0LocalCheckpointBinding,
    audited_tokenizer_snapshot: SnapshotBinding,
    protocol: ExperimentalM1ProxyProtocolConfig,
    allow_experimental_mixed_supervision: bool,
    module_importer: ModuleImporter = importlib.import_module,
) -> LoadedM1ProxyRuntime:
    """Load exact local bytes and instantiate one joint encoder."""

    if not allow_experimental_mixed_supervision:
        raise ExperimentalM1ProxyError("M1 runtime requires explicit experimental opt-in")
    manifest = cast(
        Any, _validate_protocol_against_input(protocol, prepared_input_dir=prepared_input_dir)
    )
    snapshot = verify_local_modernbert_checkpoint(checkpoint)
    if (
        checkpoint.receipt.model_id != protocol.backbone.model_id
        or checkpoint.receipt.revision != protocol.backbone.revision
        or checkpoint.receipt.tokenizer_snapshot_content_hash
        != manifest.tokenizer_decision.snapshot_content_hash
        or audited_tokenizer_snapshot.snapshot_content_hash
        != manifest.tokenizer_decision.snapshot_content_hash
        or audited_tokenizer_snapshot.model_id != protocol.backbone.model_id
        or audited_tokenizer_snapshot.revision != protocol.backbone.revision
    ):
        raise ExperimentalM1ProxyError("checkpoint/tokenizer identity differs from M1 protocol")
    tokenizer_snapshot = _verify_audited_tokenizer_snapshot(audited_tokenizer_snapshot)
    try:
        transformers = cast(Any, module_importer("transformers"))
        torch = cast(Any, module_importer("torch"))
        tokenizer = transformers.AutoTokenizer.from_pretrained(
            str(tokenizer_snapshot),
            local_files_only=True,
            trust_remote_code=False,
            use_fast=audited_tokenizer_snapshot.use_fast,
        )
        torch.manual_seed(protocol.training.seed)
        encoder = transformers.AutoModel.from_pretrained(
            str(snapshot), local_files_only=True, trust_remote_code=False
        )
        hidden_size = int(encoder.config.hidden_size)
    except Exception as exc:
        raise ExperimentalM1ProxyError("failed to load exact local ModernBERT checkpoint") from exc
    model = build_m1_cross_encoder_module(
        encoder=encoder, hidden_size=hidden_size, module_importer=module_importer
    )
    initial_state = _safetensors_state_bytes(model, module_importer=module_importer)
    examples = _load_prepared_examples(_real_directory(prepared_input_dir))
    packed, _ = _packed_tokenization(
        tokenizer, examples, selected_length=manifest.tokenizer_decision.selected_length
    )
    return LoadedM1ProxyRuntime(
        model=model,
        tokenizer=tokenizer,
        selected_length=manifest.tokenizer_decision.selected_length,
        checkpoint=checkpoint,
        audited_tokenizer_snapshot=audited_tokenizer_snapshot,
        protocol=protocol,
        initial_model_state_sha256=hashlib.sha256(initial_state).hexdigest(),
        packed_tokenization=packed,
    )


def _predict_m1_examples(
    *,
    runtime: LoadedM1ProxyRuntime,
    examples: Sequence[M0ProxyExample],
    batch_size: int,
    device: str,
    module_importer: ModuleImporter,
) -> tuple[M0ProxyPrediction, ...]:
    try:
        torch = cast(Any, module_importer("torch"))
    except (ImportError, ModuleNotFoundError) as exc:
        raise ExperimentalM1ProxyError("M1 prediction requires torch") from exc
    model = cast(Any, runtime.model)
    model.eval()
    output: list[M0ProxyPrediction] = []
    with torch.no_grad():
        for start in range(0, len(examples), batch_size):
            items = examples[start : start + batch_size]
            batch = _tensorize_m1_batch(
                tokenizer=runtime.tokenizer,
                examples=items,
                selected_length=runtime.selected_length,
                weights=[1.0] * len(items),
                device=device,
                module_importer=module_importer,
            )
            result = model(input_ids=batch["input_ids"], attention_mask=batch["attention_mask"])
            probabilities = cast(Any, result["probabilities"])
            if probabilities.ndim != 1 or probabilities.shape[0] != len(items):
                raise ExperimentalM1ProxyError("M1 prediction output shape differs")
            for item, probability in zip(items, probabilities.detach().cpu().tolist(), strict=True):
                value = float(probability)
                if not math.isfinite(value):
                    raise ExperimentalM1ProxyError("M1 produced a nonfinite probability")
                output.append(
                    M0ProxyPrediction(
                        record_id=item.record_id,
                        split=item.split,
                        pseudo_target=item.pseudo_target,
                        same_claim_probability=value,
                        private_source_content=item.private_source_content,
                    )
                )
    return tuple(sorted(output, key=lambda item: item.record_id))


def train_m1_proxy_one_epoch(
    *,
    repository_root: Path,
    prepared_input_dir: Path,
    output_dir: Path,
    checkpoint: M0LocalCheckpointBinding,
    audited_tokenizer_snapshot: SnapshotBinding,
    protocol: ExperimentalM1ProxyProtocolConfig,
    allow_experimental_mixed_supervision: bool,
    device: str = "cpu",
    module_importer: ModuleImporter = importlib.import_module,
) -> M1ProxyTrainingArtifacts:
    """Load exact dependencies and execute one immutable packed-proxy epoch."""

    runtime = load_m1_proxy_runtime(
        prepared_input_dir=prepared_input_dir,
        checkpoint=checkpoint,
        audited_tokenizer_snapshot=audited_tokenizer_snapshot,
        protocol=protocol,
        allow_experimental_mixed_supervision=allow_experimental_mixed_supervision,
        module_importer=module_importer,
    )
    return _train_loaded_m1_proxy_one_epoch(
        repository_root=repository_root,
        prepared_input_dir=prepared_input_dir,
        output_dir=output_dir,
        runtime=runtime,
        allow_experimental_mixed_supervision=allow_experimental_mixed_supervision,
        device=device,
        module_importer=module_importer,
    )


def _train_loaded_m1_proxy_one_epoch(
    *,
    repository_root: Path,
    prepared_input_dir: Path,
    output_dir: Path,
    runtime: LoadedM1ProxyRuntime,
    allow_experimental_mixed_supervision: bool,
    device: str,
    module_importer: ModuleImporter,
) -> M1ProxyTrainingArtifacts:
    if not allow_experimental_mixed_supervision:
        raise ExperimentalM1ProxyError("M1 training requires explicit experimental opt-in")
    repository = _real_directory(repository_root)
    prepared = _real_directory(prepared_input_dir)
    output = _reject_symlinks(output_dir, allow_missing=True)
    if any(_paths_overlap(output, protected) for protected in (repository, prepared)):
        raise ExperimentalM1ProxyError("M1 output must be disjoint from code and inputs")
    input_manifest = cast(
        Any, _validate_protocol_against_input(runtime.protocol, prepared_input_dir=prepared)
    )
    _verify_audited_tokenizer_snapshot(runtime.audited_tokenizer_snapshot)
    verify_local_modernbert_checkpoint(runtime.checkpoint)
    examples = _load_prepared_examples(prepared)
    trusted_tokenizer = _load_audited_tokenizer(runtime.audited_tokenizer_snapshot)
    trusted_packed, packed_lengths = _packed_tokenization(
        trusted_tokenizer, examples, selected_length=runtime.selected_length
    )
    runtime_packed, _ = _packed_tokenization(
        runtime.tokenizer, examples, selected_length=runtime.selected_length
    )
    if (
        trusted_packed != runtime.packed_tokenization
        or runtime_packed != runtime.packed_tokenization
    ):
        raise ExperimentalM1ProxyError("M1 runtime tokenizer differs from audited tokenizer")
    train_examples = _m1_schedule_examples(
        examples, packed_lengths=packed_lengths, selected_length=runtime.selected_length
    )
    schedule = build_m0_epoch_schedule(
        train_examples,
        batch_size=runtime.protocol.training.batch_size,
        seed=runtime.protocol.training.seed,
        max_unique_variants_per_ancestry=runtime.protocol.training.max_unique_variants_per_ancestry,
    )
    by_id = {item.record_id: item for item in train_examples}
    selected_rows = tuple(
        sorted(
            (item for item in schedule.records if item.selection_status == "selected"),
            key=lambda item: cast(int, item.epoch_position),
        )
    )
    selected_examples = tuple(by_id[item.record_id] for item in selected_rows)
    try:
        torch = cast(Any, module_importer("torch"))
    except (ImportError, ModuleNotFoundError) as exc:
        raise ExperimentalM1ProxyError("M1 training requires torch") from exc
    model = cast(Any, runtime.model)
    if not isinstance(model, torch.nn.Module):
        raise ExperimentalM1ProxyError("M1 training model is not a torch module")
    initial_state = _safetensors_state_bytes(model, module_importer=module_importer)
    initial_hash = hashlib.sha256(initial_state).hexdigest()
    if initial_hash != runtime.initial_model_state_sha256:
        raise ExperimentalM1ProxyError("M1 model changed after exact checkpoint loading")
    torch.manual_seed(runtime.protocol.training.seed)
    if hasattr(torch, "cuda"):
        torch.cuda.manual_seed_all(runtime.protocol.training.seed)
    torch.use_deterministic_algorithms(True)
    model.to(device=device, dtype=torch.float32)
    optimizer = torch.optim.AdamW(
        model.parameters(),
        lr=runtime.protocol.training.learning_rate,
        weight_decay=runtime.protocol.training.weight_decay,
    )
    model.train()
    weighted_loss_sum = 0.0
    step_count = 0
    batch_size = runtime.protocol.training.batch_size
    microbatch_size = runtime.protocol.training.microbatch_size
    gradient_steps = runtime.protocol.training.gradient_accumulation_steps
    for start in range(0, len(selected_examples), batch_size):
        items = selected_examples[start : start + batch_size]
        rows = selected_rows[start : start + batch_size]
        if len(items) != batch_size or len(rows) != batch_size:
            raise ExperimentalM1ProxyError("M1 encountered an incomplete effective batch")
        optimizer.zero_grad(set_to_none=True)
        logical_batch_loss = 0.0
        microbatch_count = 0
        for micro_start in range(0, batch_size, microbatch_size):
            micro_items = items[micro_start : micro_start + microbatch_size]
            micro_rows = rows[micro_start : micro_start + microbatch_size]
            batch = _tensorize_m1_batch(
                tokenizer=trusted_tokenizer,
                examples=micro_items,
                selected_length=runtime.selected_length,
                weights=[cast(float, item.loss_weight) for item in micro_rows],
                device=device,
                module_importer=module_importer,
            )
            result = model(input_ids=batch["input_ids"], attention_mask=batch["attention_mask"])
            loss = cast(
                Any,
                _weighted_binary_cross_entropy(
                    logits=result["logits"],
                    labels=batch["labels"],
                    weights=batch["weights"],
                    torch=torch,
                    normalization_weight=schedule.loss_normalization_weight,
                ),
            )
            loss.backward()
            logical_batch_loss += float(loss.detach().cpu().item())
            microbatch_count += 1
        if microbatch_count != gradient_steps:
            raise ExperimentalM1ProxyError("M1 gradient accumulation differs from protocol")
        optimizer.step()
        weighted_loss_sum += logical_batch_loss
        step_count += 1
    if step_count != schedule.batch_count:
        raise ExperimentalM1ProxyError("M1 optimizer-step count differs from schedule")
    final_state = _safetensors_state_bytes(model, module_importer=module_importer)
    final_hash = hashlib.sha256(final_state).hexdigest()
    trusted_runtime = LoadedM1ProxyRuntime(
        model=model,
        tokenizer=trusted_tokenizer,
        selected_length=runtime.selected_length,
        checkpoint=runtime.checkpoint,
        audited_tokenizer_snapshot=runtime.audited_tokenizer_snapshot,
        protocol=runtime.protocol,
        initial_model_state_sha256=runtime.initial_model_state_sha256,
        packed_tokenization=runtime.packed_tokenization,
    )
    diagnostic_examples = {
        "train": selected_examples,
        "validation": tuple(
            item
            for item in examples
            if item.split == "validation"
            and packed_lengths[item.record_id] <= runtime.selected_length
        ),
        "test": tuple(
            item
            for item in examples
            if item.split == "test" and packed_lengths[item.record_id] <= runtime.selected_length
        ),
    }
    predictions_by_split = {
        split: _predict_m1_examples(
            runtime=trusted_runtime,
            examples=diagnostic_examples[split],
            batch_size=microbatch_size,
            device=device,
            module_importer=module_importer,
        )
        for split in _SPLITS
    }
    predictions = tuple(
        sorted(
            (item for split in _SPLITS for item in predictions_by_split[split]),
            key=lambda item: item.record_id,
        )
    )
    metrics = M0ProxyTrainingMetrics(
        optimizer_steps=step_count,
        examples_exposed=len(selected_examples),
        selected_component_count=schedule.selected_component_count,
        loss_normalization_weight=schedule.loss_normalization_weight,
        mean_weighted_training_bce=weighted_loss_sum / step_count,
        initial_state_sha256=initial_hash,
        final_state_sha256=final_hash,
        diagnostics={split: _metric_set(predictions_by_split[split]) for split in _SPLITS},
    )
    non_manifest = {
        "epoch_schedule.json": canonical_json_bytes(schedule.model_dump(mode="json")) + b"\n",
        "metrics.json": canonical_json_bytes(metrics.model_dump(mode="json")) + b"\n",
        "model.safetensors": final_state,
        "predictions.jsonl": _canonical_jsonl(predictions),
    }
    code = collect_code_state(repository)
    _verify_clean_code(code)
    contains_private = any(item.private_source_content for item in examples)
    redistribution_allowed = (not contains_private) and all(
        item.redistribution_allowed for item in examples
    )
    external_allowed = (not contains_private) and all(
        item.external_transmission_allowed for item in examples
    )
    release_eligible = (not contains_private) and all(item.release_eligible for item in examples)
    manifest_data: dict[str, object] = {
        "schema_version": 1,
        "artifact_kind": "experimental_m1_proxy_training_v1",
        "prepared_input_artifact_id": input_manifest.artifact_id,
        "prepared_input_manifest_sha256": hash_file(prepared / "manifest.json"),
        "dataset_id": input_manifest.dataset_id,
        "protocol_hash": hash_canonical(runtime.protocol.model_dump(mode="json")),
        "protocol": runtime.protocol.model_dump(mode="json"),
        "code": code.model_dump(mode="json"),
        "pretrained_checkpoint": runtime.checkpoint.receipt.model_dump(mode="json"),
        "tokenizer_audit_id": input_manifest.tokenizer_decision.audit_id,
        "tokenizer_snapshot_content_hash": input_manifest.tokenizer_decision.snapshot_content_hash,
        "packed_tokenization": trusted_packed.model_dump(mode="json"),
        "runtime": _runtime_versions(device=device).model_dump(mode="json"),
        "epoch_schedule_sha256": schedule.schedule_sha256,
        "selected_record_set_sha256": schedule.selected_record_set_sha256,
        "optimizer": "AdamW",
        "learning_rate": runtime.protocol.training.learning_rate,
        "weight_decay": runtime.protocol.training.weight_decay,
        "seed": runtime.protocol.training.seed,
        "epoch_count": 1,
        "optimizer_steps": step_count,
        "examples_exposed": len(selected_examples),
        "selected_component_count": schedule.selected_component_count,
        "loss_normalization_weight": schedule.loss_normalization_weight,
        "effective_batch_size": batch_size,
        "microbatch_size": microbatch_size,
        "gradient_accumulation_steps": gradient_steps,
        "initial_model_state_sha256": runtime.initial_model_state_sha256,
        "contains_private_source_content": contains_private,
        "redistribution_allowed": redistribution_allowed,
        "external_transmission_allowed": external_allowed,
        "release_eligible": release_eligible,
        "output_sha256": {
            name: hashlib.sha256(payload).hexdigest() for name, payload in non_manifest.items()
        },
        "model_weights_loaded": True,
        "training_executed": True,
        **ExperimentalM0ProxyBoundary().model_dump(mode="json"),
    }
    manifest = M1ProxyTrainingManifest.model_validate(
        {
            **manifest_data,
            "artifact_id": "experimental-m1-proxy-training:" + hash_canonical(manifest_data),
        }
    )
    payloads = {
        **non_manifest,
        "manifest.json": canonical_json_bytes(manifest.model_dump(mode="json")) + b"\n",
    }
    replayed = _write_or_replay_training(output, payloads)
    verify_m1_proxy_training(output)
    return M1ProxyTrainingArtifacts(
        output_dir=output,
        artifact_id=manifest.artifact_id,
        optimizer_steps=step_count,
        examples_exposed=len(selected_examples),
        replayed=replayed,
    )


def verify_m1_proxy_training(
    output_dir: Path,
    *,
    repository_root: Path | None = None,
    prepared_input_dir: Path | None = None,
    checkpoint: M0LocalCheckpointBinding | None = None,
    audited_tokenizer_snapshot: SnapshotBinding | None = None,
    protocol: ExperimentalM1ProxyProtocolConfig | None = None,
) -> M1ProxyTrainingManifest:
    """Verify internal bytes, accounting, and optional exact external bindings."""

    root = _real_directory(output_dir)
    if {path.name for path in root.iterdir()} != _OUTPUT_FILES:
        raise ExperimentalM1ProxyError("M1 training output file set is not exact")
    manifest = M1ProxyTrainingManifest.model_validate(_strict_json(root / "manifest.json"))
    for name, expected in manifest.output_sha256.items():
        if hash_file(_regular_file(root / name)) != expected:
            raise ExperimentalM1ProxyError(f"M1 training output hash differs: {name}")
    schedule = M0EpochSchedule.model_validate(_strict_json(root / "epoch_schedule.json"))
    if (
        schedule.schedule_sha256 != manifest.epoch_schedule_sha256
        or schedule.selected_record_set_sha256 != manifest.selected_record_set_sha256
        or schedule.batch_count != manifest.optimizer_steps
        or schedule.selected_count != manifest.examples_exposed
        or schedule.selected_component_count != manifest.selected_component_count
        or not math.isclose(
            schedule.loss_normalization_weight,
            manifest.loss_normalization_weight,
            rel_tol=0.0,
            abs_tol=1e-12,
        )
    ):
        raise ExperimentalM1ProxyError("M1 schedule differs from manifest")
    predictions: list[M0ProxyPrediction] = []
    with _regular_file(root / "predictions.jsonl").open("rb") as handle:
        for line_number, raw in enumerate(handle, start=1):
            try:
                item = M0ProxyPrediction.model_validate(json.loads(raw))
            except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
                raise ExperimentalM1ProxyError(
                    f"invalid M1 prediction at line {line_number}"
                ) from exc
            if raw != canonical_json_bytes(item.model_dump(mode="json")) + b"\n":
                raise ExperimentalM1ProxyError(f"noncanonical M1 prediction at line {line_number}")
            predictions.append(item)
    if len({item.record_id for item in predictions}) != len(predictions):
        raise ExperimentalM1ProxyError("M1 predictions repeat a record")
    metrics = M0ProxyTrainingMetrics.model_validate(_strict_json(root / "metrics.json"))
    grouped = {
        split: tuple(item for item in predictions if item.split == split) for split in _SPLITS
    }
    if metrics.diagnostics != {split: _metric_set(grouped[split]) for split in _SPLITS}:
        raise ExperimentalM1ProxyError("M1 metrics differ from predictions")
    if (
        metrics.optimizer_steps != manifest.optimizer_steps
        or metrics.examples_exposed != manifest.examples_exposed
        or metrics.selected_component_count != manifest.selected_component_count
        or not math.isclose(
            metrics.loss_normalization_weight,
            manifest.loss_normalization_weight,
            rel_tol=0.0,
            abs_tol=1e-12,
        )
        or metrics.initial_state_sha256 != manifest.initial_model_state_sha256
        or metrics.final_state_sha256 != manifest.output_sha256["model.safetensors"]
    ):
        raise ExperimentalM1ProxyError("M1 metrics differ from manifest")
    try:
        safetensors_torch = cast(Any, importlib.import_module("safetensors.torch"))
        state = safetensors_torch.load(_regular_file(root / "model.safetensors").read_bytes())
    except Exception as exc:
        raise ExperimentalM1ProxyError("M1 output is not a valid safetensors checkpoint") from exc
    if not isinstance(state, dict) or not state:
        raise ExperimentalM1ProxyError("M1 safetensors checkpoint has no state")
    for name in ("manifest.json", "metrics.json", "epoch_schedule.json", "predictions.jsonl"):
        payload = _regular_file(root / name).read_bytes()
        # The public tags are embedded in the frozen architecture protocol;
        # actual model-visible statements always begin with this marker.
        if b"[HEADLESS]\n" in payload:
            raise ExperimentalM1ProxyError("M1 training metadata leaks model-visible source text")
    if (
        repository_root is not None
        and collect_code_state(_real_directory(repository_root)) != manifest.code
    ):
        raise ExperimentalM1ProxyError("current code differs from M1 training freeze")
    external = (prepared_input_dir, checkpoint, audited_tokenizer_snapshot, protocol)
    if any(value is not None for value in external):
        if any(value is None for value in external):
            raise ExperimentalM1ProxyError(
                "exact M1 verification requires inputs, checkpoint, tokenizer, and protocol"
            )
        assert prepared_input_dir is not None
        assert checkpoint is not None
        assert audited_tokenizer_snapshot is not None
        assert protocol is not None
        prepared = _real_directory(prepared_input_dir)
        prepared_manifest = cast(
            Any, _validate_protocol_against_input(protocol, prepared_input_dir=prepared)
        )
        if (
            prepared_manifest.artifact_id != manifest.prepared_input_artifact_id
            or hash_file(prepared / "manifest.json") != manifest.prepared_input_manifest_sha256
            or prepared_manifest.dataset_id != manifest.dataset_id
            or protocol != manifest.protocol
        ):
            raise ExperimentalM1ProxyError("prepared M1 dependencies differ from manifest")
        _verify_audited_tokenizer_snapshot(audited_tokenizer_snapshot)
        if (
            audited_tokenizer_snapshot.snapshot_content_hash
            != manifest.tokenizer_snapshot_content_hash
            or audited_tokenizer_snapshot.model_id != manifest.pretrained_checkpoint.model_id
            or audited_tokenizer_snapshot.revision != manifest.pretrained_checkpoint.revision
            or checkpoint.receipt != manifest.pretrained_checkpoint
        ):
            raise ExperimentalM1ProxyError("M1 tokenizer/checkpoint differs from manifest")
        verify_local_modernbert_checkpoint(checkpoint)
        tokenizer = _load_audited_tokenizer(audited_tokenizer_snapshot)
        examples = _load_prepared_examples(prepared)
        packed, lengths = _packed_tokenization(
            tokenizer,
            examples,
            selected_length=prepared_manifest.tokenizer_decision.selected_length,
        )
        if packed != manifest.packed_tokenization:
            raise ExperimentalM1ProxyError("M1 packed tokenization differs from manifest")
        schedule_examples = _m1_schedule_examples(
            examples,
            packed_lengths=lengths,
            selected_length=prepared_manifest.tokenizer_decision.selected_length,
        )
        rebuilt = build_m0_epoch_schedule(
            schedule_examples,
            batch_size=protocol.training.batch_size,
            seed=protocol.training.seed,
            max_unique_variants_per_ancestry=protocol.training.max_unique_variants_per_ancestry,
        )
        if rebuilt != schedule:
            raise ExperimentalM1ProxyError("M1 schedule does not replay from exact inputs")
    return manifest


__all__ = [
    "ExperimentalM1ProxyError",
    "ExperimentalM1ProxyProtocolConfig",
    "LoadedM1ProxyRuntime",
    "M1PackedTokenization",
    "M1ProxyArchitectureProtocol",
    "M1ProxyTrainingArtifacts",
    "M1ProxyTrainingManifest",
    "build_m1_cross_encoder_module",
    "load_experimental_m1_proxy_config",
    "load_m1_proxy_runtime",
    "pack_m1_pair",
    "train_m1_proxy_one_epoch",
    "verify_m1_proxy_training",
]
