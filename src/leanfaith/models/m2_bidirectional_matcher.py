"""Executable M2 bidirectional-matcher proxy diagnostic.

M2 is the first LeanFaith proxy model that performs token-level interaction
between independently encoded statements.  One shared pretrained encoder is
called once per side.  Two distinct matching layers then update both sides
synchronously; within each layer the exact same parameters are used for
``A <- B`` and ``B <- A``.  The same-claim head sees only commutative pooled
features, so its deterministic evaluation output is invariant to input swap.

This remains machine-proxy engineering, not semantic training or evaluation.
The current corpus contains only binary pseudo-targets, therefore relation,
ambiguity, localization, repair, and auxiliary heads are deliberately absent.
The full Revision 4.1 ``HEADLESS + SIGNATURE_EXPLICIT`` bundle also remains a
scientific-data milestone; this bounded proxy reuses the exact headless-only M0
input artifact instead of inventing an unavailable representation.
"""

from __future__ import annotations

import errno
import hashlib
import importlib
import json
import math
import os
import shutil
import stat
import tempfile
from collections.abc import Mapping, Sequence
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
    M0ProxyMetricSet,
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
    _prepared_tokenization_sha256,
    _real_directory,
    _reject_symlinks,
    _runtime_versions,
    _safetensors_state_bytes,
    _tensorize_m0_batch,
    _verify_audited_tokenizer_snapshot,
    _verify_clean_code,
    _weighted_binary_cross_entropy,
    build_m0_epoch_schedule,
    verify_experimental_m0_proxy_inputs,
    verify_local_modernbert_checkpoint,
)
from leanfaith.models.tokenizer_audit import SnapshotBinding
from leanfaith.schemas.manifest import CodeState, collect_code_state

_HEX64 = r"^[0-9a-f]{64}$"
_ARTIFACT_ID = r"^experimental-m2-proxy-training:[0-9a-f]{64}$"
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
        "swap_invariance.json",
    }
)
_NON_MANIFEST_OUTPUTS = _OUTPUT_FILES - {"manifest.json"}
_SPLITS = ("test", "train", "validation")


class ExperimentalM2ProxyError(ExperimentalM0ProxyError):
    """An M2 proxy prerequisite, policy, or replay invariant failed."""


def _sigmoid(value: float) -> float:
    """Numerically stable scalar sigmoid used by the portable prediction schema."""

    if value >= 0.0:
        inverse = math.exp(-value)
        return 1.0 / (1.0 + inverse)
    exponent = math.exp(value)
    return exponent / (1.0 + exponent)


class M2ProxyArchitectureProtocol(StrictModel):
    """Frozen architectural assertions for the bounded M2 proxy."""

    architecture: Literal["shared_encoder_two_layer_synchronous_bidirectional_matcher_v1"] = (
        "shared_encoder_two_layer_synchronous_bidirectional_matcher_v1"
    )
    input_profile: Literal["headless_only_proxy_v1"] = "headless_only_proxy_v1"
    encoder_instances: Literal[1] = 1
    encoder_calls_per_pair: Literal[2] = 2
    matching_layer_count: Literal[2] = 2
    attention_head_count: Literal[8] = 8
    directional_parameters_shared: Literal[True] = True
    synchronous_directional_updates: Literal[True] = True
    matcher_dropout: float = Field(default=0.0, ge=0.0, le=0.0)
    pooling: Literal["attention_masked_mean"] = "attention_masked_mean"
    symmetric_features: tuple[str, ...] = ("sum", "absolute_difference", "product")
    same_claim_head: Literal["symmetric_single_linear_logit"] = "symmetric_single_linear_logit"
    same_claim_swap_invariant: Literal[True] = True
    swap_check_record_count: Literal[64] = 64
    swap_check_atol: float = Field(default=1e-7, ge=1e-7, le=1e-7)
    prediction_replay_atol: float = Field(default=1e-7, ge=1e-7, le=1e-7)
    relation_head_enabled: Literal[False] = False
    relation_head_disabled_reason: Literal[
        "binary_proxy_targets_do_not_support_directional_relations"
    ] = "binary_proxy_targets_do_not_support_directional_relations"
    ambiguity_head_enabled: Literal[False] = False
    auxiliary_heads_enabled: Literal[False] = False
    reference_base_encoding_cacheable: Literal[True] = True
    candidate_dependent_matching_cacheable: Literal[False] = False

    @field_validator("symmetric_features")
    @classmethod
    def _features_are_exact(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        expected = ("sum", "absolute_difference", "product")
        if value != expected:
            raise ValueError(f"M2 symmetric features must be exactly {expected}")
        return value


class ExperimentalM2ProxyProtocolConfig(ExperimentalM0ProxyBoundary):
    """Frozen M2 proxy protocol; M0 owns the exact prepared input bundle."""

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
    architecture: M2ProxyArchitectureProtocol
    training: M0ProxyTrainingProtocol

    @field_validator("backbone_registry_path")
    @classmethod
    def _registry_path_is_relative(cls, value: str) -> str:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("backbone_registry_path must be repository-relative")
        return value


def load_experimental_m2_proxy_config(
    path: Path,
) -> LoadedConfig[ExperimentalM2ProxyProtocolConfig]:
    """Load the frozen M2 proxy protocol."""

    return load_config(path, ExperimentalM2ProxyProtocolConfig)


class M2SwapInvarianceCheck(StrictModel):
    """Text-free checkpoint assertion for the required M2 swap equivariance."""

    schema_version: Literal[1] = 1
    record_count: int = Field(gt=0)
    record_set_sha256: str = Field(pattern=_HEX64)
    absolute_tolerance: float = Field(ge=0.0)
    maximum_equivalence_probability_difference: float = Field(ge=0.0)
    maximum_equivalence_logit_difference: float = Field(ge=0.0)
    equivalence_swap_invariant: Literal[True] = True
    ambiguity_head_enabled: Literal[False] = False
    relation_head_enabled: Literal[False] = False
    directional_relation_swap_check: Literal["not_applicable_binary_proxy_targets"] = (
        "not_applicable_binary_proxy_targets"
    )

    @model_validator(mode="after")
    def _differences_are_within_tolerance(self) -> M2SwapInvarianceCheck:
        for value in (
            self.maximum_equivalence_probability_difference,
            self.maximum_equivalence_logit_difference,
        ):
            if not math.isfinite(value) or value > self.absolute_tolerance:
                raise ValueError("M2 equivalence output is not swap invariant")
        return self


class M2ProxyPrediction(StrictModel):
    """Text-free M2 prediction bound to one checkpoint logit."""

    schema_version: Literal[1] = 1
    record_id: str = Field(pattern=r"^experimental_mixed_pair:[0-9a-f]{64}$")
    split: Literal["train", "validation", "test"]
    pseudo_target: Literal["same_claim", "not_same_claim"]
    same_claim_logit: float
    same_claim_probability: float = Field(ge=0.0, le=1.0)
    private_source_content: bool

    @model_validator(mode="after")
    def _probability_matches_logit(self) -> M2ProxyPrediction:
        if not math.isfinite(self.same_claim_logit):
            raise ValueError("M2 prediction logit must be finite")
        expected = _sigmoid(self.same_claim_logit)
        if not math.isclose(
            self.same_claim_probability,
            expected,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ValueError("M2 prediction probability differs from its logit")
        return self


class M2ProxyTrainingManifest(ExperimentalM0ProxyBoundary):
    """Portable, content-addressed record of one M2 proxy epoch."""

    schema_version: Literal[1] = 1
    artifact_id: str = Field(pattern=_ARTIFACT_ID)
    artifact_kind: Literal["experimental_m2_proxy_training_v1"] = (
        "experimental_m2_proxy_training_v1"
    )
    prepared_input_artifact_id: str = Field(pattern=_M0_INPUT_ID)
    prepared_input_manifest_sha256: str = Field(pattern=_HEX64)
    dataset_id: str = Field(pattern=_DATASET_ID)
    protocol_hash: str = Field(pattern=_HEX64)
    protocol: ExperimentalM2ProxyProtocolConfig
    code: CodeState
    pretrained_checkpoint: M0OfficialCheckpointReceipt
    tokenizer_audit_id: str = Field(pattern=_AUDIT_ID)
    tokenizer_snapshot_content_hash: str = Field(pattern=_HEX64)
    prepared_tokenization_sha256: str = Field(pattern=_HEX64)
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
    swap_invariance: M2SwapInvarianceCheck
    contains_private_source_content: bool
    redistribution_allowed: bool
    external_transmission_allowed: bool
    release_eligible: bool
    output_sha256: dict[str, str]
    model_weights_loaded: Literal[True] = True
    training_executed: Literal[True] = True

    @model_validator(mode="after")
    def _manifest_is_coherent(self) -> M2ProxyTrainingManifest:
        if self.protocol_hash != hash_canonical(self.protocol.model_dump(mode="json")):
            raise ValueError("M2 protocol hash differs from embedded protocol")
        training = self.protocol.training
        if (
            self.optimizer != training.optimizer
            or self.learning_rate != training.learning_rate
            or self.weight_decay != training.weight_decay
            or self.seed != training.seed
            or self.epoch_count != training.epochs
            or self.effective_batch_size != training.batch_size
            or self.microbatch_size != training.microbatch_size
            or self.gradient_accumulation_steps != training.gradient_accumulation_steps
        ):
            raise ValueError("M2 training fields differ from embedded protocol")
        receipt = self.pretrained_checkpoint
        backbone = self.protocol.backbone
        expected_weight = receipt.required_files.get(backbone.weight_filename)
        if (
            receipt.model_id != backbone.model_id
            or receipt.revision != backbone.revision
            or receipt.hf_snapshot_api_url != backbone.hf_snapshot_api_url
            or receipt.tokenizer_snapshot_content_hash != self.tokenizer_snapshot_content_hash
            or expected_weight is None
            or expected_weight.sha256 != backbone.weight_sha256
            or expected_weight.byte_count != backbone.weight_byte_count
        ):
            raise ValueError("M2 checkpoint/tokenizer fields differ from protocol")
        if self.code.git_dirty or self.code.code_tree_hash is None or self.code.untracked_files:
            raise ValueError("M2 proxy training requires clean fully tracked code")
        if set(self.output_sha256) != _NON_MANIFEST_OUTPUTS:
            raise ValueError("M2 training manifest output set is not exact")
        if self.swap_invariance.absolute_tolerance != self.protocol.architecture.swap_check_atol:
            raise ValueError("M2 swap tolerance differs from protocol")
        if self.swap_invariance.record_count > self.protocol.architecture.swap_check_record_count:
            raise ValueError("M2 swap-check count exceeds protocol")
        if self.contains_private_source_content and (
            self.redistribution_allowed
            or self.external_transmission_allowed
            or self.release_eligible
        ):
            raise ValueError("private-trained M2 artifact cannot be shared or released")
        if self.release_eligible and not self.redistribution_allowed:
            raise ValueError("M2 release policy is incoherent")
        if self.microbatch_size * self.gradient_accumulation_steps != self.effective_batch_size:
            raise ValueError("M2 training microbatch accounting differs")
        expected = "experimental-m2-proxy-training:" + hash_canonical(
            self.model_dump(mode="json", exclude={"artifact_id"})
        )
        if self.artifact_id != expected:
            raise ValueError("M2 training artifact ID differs from canonical content")
        return self


@dataclass(frozen=True, slots=True)
class M2ProxyTrainingArtifacts:
    output_dir: Path
    artifact_id: str
    optimizer_steps: int
    examples_exposed: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class LoadedM2ProxyRuntime:
    model: object
    tokenizer: object
    selected_length: Literal[512, 1024]
    checkpoint: M0LocalCheckpointBinding
    audited_tokenizer_snapshot: SnapshotBinding
    protocol: ExperimentalM2ProxyProtocolConfig
    initial_model_state_sha256: str
    prepared_tokenization_sha256: str


def _validate_protocol_against_input(
    protocol: ExperimentalM2ProxyProtocolConfig,
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
        raise ExperimentalM2ProxyError("M2 protocol differs from frozen M0 input bindings")
    return manifest


def build_m2_bidirectional_matcher_module(
    *,
    encoder: object,
    hidden_size: int,
    attention_head_count: int = 8,
    module_importer: ModuleImporter = importlib.import_module,
) -> object:
    """Build the shared encoder and two synchronous bidirectional matchers."""

    if hidden_size <= 0 or attention_head_count <= 0:
        raise ExperimentalM2ProxyError("M2 hidden size and attention heads must be positive")
    if hidden_size % attention_head_count:
        raise ExperimentalM2ProxyError("M2 hidden size must be divisible by attention heads")
    try:
        torch = cast(Any, module_importer("torch"))
    except (ImportError, ModuleNotFoundError) as exc:
        raise ExperimentalM2ProxyError("M2 requires the optional torch runtime") from exc
    if not isinstance(encoder, torch.nn.Module):
        raise ExperimentalM2ProxyError("M2 encoder must be a torch.nn.Module")

    class _SharedDirectionalMatchLayer(torch.nn.Module):  # type: ignore[misc, name-defined]
        def __init__(self, width: int, heads: int) -> None:
            super().__init__()
            self.cross_attention = torch.nn.MultiheadAttention(
                width,
                heads,
                dropout=0.0,
                batch_first=True,
            )
            self.attention_norm = torch.nn.LayerNorm(width)
            self.feed_forward = torch.nn.Sequential(
                torch.nn.Linear(width, 4 * width),
                torch.nn.GELU(),
                torch.nn.Linear(4 * width, width),
            )
            self.output_norm = torch.nn.LayerNorm(width)

        def forward(
            self,
            query_states: Any,
            key_value_states: Any,
            *,
            query_attention_mask: Any,
            key_value_attention_mask: Any,
        ) -> Any:
            attended, _ = self.cross_attention(
                query_states,
                key_value_states,
                key_value_states,
                key_padding_mask=~key_value_attention_mask.to(dtype=torch.bool),
                need_weights=False,
            )
            hidden = self.attention_norm(query_states + attended)
            hidden = self.output_norm(hidden + self.feed_forward(hidden))
            return hidden * query_attention_mask.to(dtype=hidden.dtype).unsqueeze(-1)

    class _M2BidirectionalMatcher(torch.nn.Module):  # type: ignore[misc, name-defined]
        def __init__(self, shared_encoder: object, width: int, heads: int) -> None:
            super().__init__()
            self.encoder = cast(Any, shared_encoder)
            self.hidden_size = width
            self.matching_layers = torch.nn.ModuleList(
                [_SharedDirectionalMatchLayer(width, heads) for _ in range(2)]
            )
            self.same_claim_head = torch.nn.Linear(3 * width, 1)

        def encode_base(self, *, input_ids: Any, attention_mask: Any) -> Any:
            if input_ids.ndim != 2 or attention_mask.ndim != 2:
                raise ValueError("M2 input IDs and attention masks must be rank-two")
            if input_ids.shape != attention_mask.shape:
                raise ValueError("M2 input IDs and attention masks differ in shape")
            if bool((attention_mask.sum(dim=1) <= 0).any().item()):
                raise ValueError("M2 attention mask contains an empty sequence")
            output = self.encoder(input_ids=input_ids, attention_mask=attention_mask)
            hidden = getattr(output, "last_hidden_state", None)
            if hidden is None:
                try:
                    hidden = output[0]
                except (KeyError, IndexError, TypeError) as exc:
                    raise ValueError("M2 encoder output lacks last_hidden_state") from exc
            if hidden.ndim != 3 or hidden.shape[:2] != input_ids.shape:
                raise ValueError("M2 encoder hidden state has an incompatible shape")
            if hidden.shape[-1] != self.hidden_size:
                raise ValueError("M2 encoder hidden width differs from configured hidden_size")
            return hidden * attention_mask.to(dtype=hidden.dtype).unsqueeze(-1)

        @staticmethod
        def _masked_mean(hidden: Any, attention_mask: Any) -> Any:
            mask = attention_mask.to(dtype=hidden.dtype).unsqueeze(-1)
            return (hidden * mask).sum(dim=1) / mask.sum(dim=1).clamp_min(1.0)

        def match_encoded(
            self,
            *,
            source_hidden: Any,
            source_attention_mask: Any,
            candidate_hidden: Any,
            candidate_attention_mask: Any,
        ) -> dict[str, Any]:
            if source_hidden.ndim != 3 or candidate_hidden.ndim != 3:
                raise ValueError("M2 encoded states must be rank-three")
            if source_hidden.shape[0] != candidate_hidden.shape[0]:
                raise ValueError("M2 encoded sides have different batch sizes")
            if (
                source_hidden.shape[-1] != self.hidden_size
                or candidate_hidden.shape[-1] != self.hidden_size
            ):
                raise ValueError("M2 encoded states have an incompatible width")
            source_states = source_hidden
            candidate_states = candidate_hidden
            for layer in self.matching_layers:
                # Both updates read the previous layer's states.  The one layer
                # object is called in both directions, so directional weights
                # are shared without sequential state contamination.
                previous_source = source_states
                previous_candidate = candidate_states
                next_source = layer(
                    previous_source,
                    previous_candidate,
                    query_attention_mask=source_attention_mask,
                    key_value_attention_mask=candidate_attention_mask,
                )
                next_candidate = layer(
                    previous_candidate,
                    previous_source,
                    query_attention_mask=candidate_attention_mask,
                    key_value_attention_mask=source_attention_mask,
                )
                source_states, candidate_states = next_source, next_candidate
            source_pooled = self._masked_mean(source_states, source_attention_mask)
            candidate_pooled = self._masked_mean(candidate_states, candidate_attention_mask)
            symmetric = torch.cat(
                (
                    source_pooled + candidate_pooled,
                    torch.abs(source_pooled - candidate_pooled),
                    source_pooled * candidate_pooled,
                ),
                dim=-1,
            )
            logits = self.same_claim_head(symmetric).squeeze(-1)
            return {
                "logits": logits,
                "probabilities": torch.sigmoid(logits),
                "symmetric_features": symmetric,
                "source_matched_embeddings": source_pooled,
                "candidate_matched_embeddings": candidate_pooled,
            }

        def forward(
            self,
            *,
            source_input_ids: Any,
            source_attention_mask: Any,
            candidate_input_ids: Any,
            candidate_attention_mask: Any,
        ) -> dict[str, Any]:
            source_hidden = self.encode_base(
                input_ids=source_input_ids,
                attention_mask=source_attention_mask,
            )
            candidate_hidden = self.encode_base(
                input_ids=candidate_input_ids,
                attention_mask=candidate_attention_mask,
            )
            return self.match_encoded(
                source_hidden=source_hidden,
                source_attention_mask=source_attention_mask,
                candidate_hidden=candidate_hidden,
                candidate_attention_mask=candidate_attention_mask,
            )

    _M2BidirectionalMatcher.__name__ = "M2BidirectionalMatcher"
    return _M2BidirectionalMatcher(encoder, hidden_size, attention_head_count)


def load_m2_proxy_runtime(
    *,
    prepared_input_dir: Path,
    checkpoint: M0LocalCheckpointBinding,
    audited_tokenizer_snapshot: SnapshotBinding,
    protocol: ExperimentalM2ProxyProtocolConfig,
    allow_experimental_mixed_supervision: bool,
    module_importer: ModuleImporter = importlib.import_module,
) -> LoadedM2ProxyRuntime:
    """Load exact local bytes and instantiate the frozen M2 architecture."""

    if not allow_experimental_mixed_supervision:
        raise ExperimentalM2ProxyError("M2 runtime requires explicit experimental opt-in")
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
        raise ExperimentalM2ProxyError("checkpoint/tokenizer identity differs from M2 protocol")
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
        raise ExperimentalM2ProxyError("failed to load exact local ModernBERT checkpoint") from exc
    model = build_m2_bidirectional_matcher_module(
        encoder=encoder,
        hidden_size=hidden_size,
        attention_head_count=protocol.architecture.attention_head_count,
        module_importer=module_importer,
    )
    initial_state = _safetensors_state_bytes(model, module_importer=module_importer)
    examples = _load_prepared_examples(_real_directory(prepared_input_dir))
    prepared_tokenization = _prepared_tokenization_sha256(tokenizer, examples)
    return LoadedM2ProxyRuntime(
        model=model,
        tokenizer=tokenizer,
        selected_length=manifest.tokenizer_decision.selected_length,
        checkpoint=checkpoint,
        audited_tokenizer_snapshot=audited_tokenizer_snapshot,
        protocol=protocol,
        initial_model_state_sha256=hashlib.sha256(initial_state).hexdigest(),
        prepared_tokenization_sha256=prepared_tokenization,
    )


def _predict_m2_examples(
    *,
    runtime: LoadedM2ProxyRuntime,
    examples: Sequence[M0ProxyExample],
    batch_size: int,
    device: str,
    module_importer: ModuleImporter,
) -> tuple[M2ProxyPrediction, ...]:
    try:
        torch = cast(Any, module_importer("torch"))
    except (ImportError, ModuleNotFoundError) as exc:
        raise ExperimentalM2ProxyError("M2 prediction requires torch") from exc
    model = cast(Any, runtime.model)
    model.eval()
    output: list[M2ProxyPrediction] = []
    with torch.no_grad():
        for start in range(0, len(examples), batch_size):
            items = examples[start : start + batch_size]
            batch = _tensorize_m0_batch(
                tokenizer=runtime.tokenizer,
                examples=items,
                selected_length=runtime.selected_length,
                weights=[1.0] * len(items),
                device=device,
                module_importer=module_importer,
            )
            result = model(
                source_input_ids=batch["source_input_ids"],
                source_attention_mask=batch["source_attention_mask"],
                candidate_input_ids=batch["candidate_input_ids"],
                candidate_attention_mask=batch["candidate_attention_mask"],
            )
            logits = cast(Any, result["logits"])
            if logits.ndim != 1 or logits.shape[0] != len(items):
                raise ExperimentalM2ProxyError("M2 prediction output shape differs")
            for item, raw_logit in zip(items, logits.detach().cpu().tolist(), strict=True):
                logit = float(raw_logit)
                if not math.isfinite(logit):
                    raise ExperimentalM2ProxyError("M2 produced a nonfinite logit")
                output.append(
                    M2ProxyPrediction(
                        record_id=item.record_id,
                        split=item.split,
                        pseudo_target=item.pseudo_target,
                        same_claim_logit=logit,
                        same_claim_probability=_sigmoid(logit),
                        private_source_content=item.private_source_content,
                    )
                )
    return tuple(sorted(output, key=lambda item: item.record_id))


def _m2_metric_set(predictions: Sequence[M2ProxyPrediction]) -> M0ProxyMetricSet:
    """Reuse the frozen binary metric implementation without dropping M2 logits on disk."""

    compatible = tuple(
        M0ProxyPrediction(
            record_id=item.record_id,
            split=item.split,
            pseudo_target=item.pseudo_target,
            same_claim_probability=item.same_claim_probability,
            private_source_content=item.private_source_content,
        )
        for item in predictions
    )
    return _metric_set(compatible)


def _swap_check_examples(
    examples: Sequence[M0ProxyExample], *, count: int
) -> tuple[M0ProxyExample, ...]:
    canonical = tuple(sorted(examples, key=lambda item: item.record_id))
    if not canonical or count <= 0:
        raise ExperimentalM2ProxyError("M2 swap check requires examples and a positive count")
    return canonical[: min(count, len(canonical))]


def _check_swap_invariance(
    *,
    runtime: LoadedM2ProxyRuntime,
    examples: Sequence[M0ProxyExample],
    device: str,
    module_importer: ModuleImporter,
) -> M2SwapInvarianceCheck:
    try:
        torch = cast(Any, module_importer("torch"))
    except (ImportError, ModuleNotFoundError) as exc:
        raise ExperimentalM2ProxyError("M2 swap check requires torch") from exc
    selected = _swap_check_examples(
        examples, count=runtime.protocol.architecture.swap_check_record_count
    )
    model = cast(Any, runtime.model)
    model.eval()
    maximum_probability = 0.0
    maximum_logit = 0.0
    with torch.no_grad():
        for item in selected:
            batch = _tensorize_m0_batch(
                tokenizer=runtime.tokenizer,
                examples=(item,),
                selected_length=runtime.selected_length,
                weights=(1.0,),
                device=device,
                module_importer=module_importer,
            )
            direct = model(
                source_input_ids=batch["source_input_ids"],
                source_attention_mask=batch["source_attention_mask"],
                candidate_input_ids=batch["candidate_input_ids"],
                candidate_attention_mask=batch["candidate_attention_mask"],
            )
            swapped = model(
                source_input_ids=batch["candidate_input_ids"],
                source_attention_mask=batch["candidate_attention_mask"],
                candidate_input_ids=batch["source_input_ids"],
                candidate_attention_mask=batch["source_attention_mask"],
            )
            probability_difference = float(
                torch.max(torch.abs(direct["probabilities"] - swapped["probabilities"]))
                .detach()
                .cpu()
                .item()
            )
            logit_difference = float(
                torch.max(torch.abs(direct["logits"] - swapped["logits"])).detach().cpu().item()
            )
            maximum_probability = max(maximum_probability, probability_difference)
            maximum_logit = max(maximum_logit, logit_difference)
    record_ids = tuple(item.record_id for item in selected)
    return M2SwapInvarianceCheck(
        record_count=len(selected),
        record_set_sha256=hash_canonical(
            {"schema": "m2_swap_check_record_set_v1", "record_ids": record_ids}
        ),
        absolute_tolerance=runtime.protocol.architecture.swap_check_atol,
        maximum_equivalence_probability_difference=maximum_probability,
        maximum_equivalence_logit_difference=maximum_logit,
    )


def _require_prediction_replay(
    stored: Sequence[M2ProxyPrediction],
    replayed: Sequence[M2ProxyPrediction],
    *,
    absolute_tolerance: float,
) -> None:
    """Bind every portable prediction to inference from the final checkpoint."""

    if len(stored) != len(replayed):
        raise ExperimentalM2ProxyError("M2 checkpoint replay prediction count differs")
    for expected, observed in zip(stored, replayed, strict=True):
        if (
            expected.record_id != observed.record_id
            or expected.split != observed.split
            or expected.pseudo_target != observed.pseudo_target
            or expected.private_source_content != observed.private_source_content
        ):
            raise ExperimentalM2ProxyError("M2 checkpoint replay prediction identity differs")
        if (
            abs(expected.same_claim_logit - observed.same_claim_logit) > absolute_tolerance
            or abs(expected.same_claim_probability - observed.same_claim_probability)
            > absolute_tolerance
        ):
            raise ExperimentalM2ProxyError("M2 predictions do not replay from final checkpoint")


def _require_replayed_diagnostics(
    stored: Mapping[str, M0ProxyMetricSet],
    replayed_predictions: Sequence[M2ProxyPrediction],
    *,
    absolute_tolerance: float,
) -> None:
    """Recompute and compare all diagnostic metrics from checkpoint inference."""

    replayed_by_split = {
        split: tuple(item for item in replayed_predictions if item.split == split)
        for split in _SPLITS
    }
    replayed = {split: _m2_metric_set(replayed_by_split[split]) for split in _SPLITS}
    if tuple(stored) != _SPLITS:
        raise ExperimentalM2ProxyError("M2 stored diagnostic split set is not exact")
    for split in _SPLITS:
        expected = stored[split]
        observed = replayed[split]
        if (
            expected.record_count != observed.record_count
            or expected.same_claim_count != observed.same_claim_count
            or expected.not_same_claim_count != observed.not_same_claim_count
        ):
            raise ExperimentalM2ProxyError("M2 replayed diagnostic counts differ")
        for field in ("weighted_bce", "auprc", "balanced_accuracy"):
            if (
                abs(cast(float, getattr(expected, field)) - cast(float, getattr(observed, field)))
                > absolute_tolerance
            ):
                raise ExperimentalM2ProxyError(
                    f"M2 replayed diagnostic metric differs: {split}.{field}"
                )


def train_m2_proxy_one_epoch(
    *,
    repository_root: Path,
    prepared_input_dir: Path,
    output_dir: Path,
    checkpoint: M0LocalCheckpointBinding,
    audited_tokenizer_snapshot: SnapshotBinding,
    protocol: ExperimentalM2ProxyProtocolConfig,
    allow_experimental_mixed_supervision: bool,
    device: str = "cpu",
    module_importer: ModuleImporter = importlib.import_module,
) -> M2ProxyTrainingArtifacts:
    """Load exact dependencies and execute one immutable M2 proxy epoch."""

    runtime = load_m2_proxy_runtime(
        prepared_input_dir=prepared_input_dir,
        checkpoint=checkpoint,
        audited_tokenizer_snapshot=audited_tokenizer_snapshot,
        protocol=protocol,
        allow_experimental_mixed_supervision=allow_experimental_mixed_supervision,
        module_importer=module_importer,
    )
    return _train_loaded_m2_proxy_one_epoch(
        repository_root=repository_root,
        prepared_input_dir=prepared_input_dir,
        output_dir=output_dir,
        runtime=runtime,
        allow_experimental_mixed_supervision=allow_experimental_mixed_supervision,
        device=device,
        module_importer=module_importer,
    )


def _write_or_replay_m2_training(output_dir: Path, payloads: Mapping[str, bytes]) -> bool:
    """Atomically publish or exactly replay the M2-specific output set."""

    if set(payloads) != _OUTPUT_FILES:
        raise ExperimentalM2ProxyError("M2 training payload set is not exact")
    output = _m2_reject_symlinks(output_dir, allow_missing=True)

    def verify_existing() -> bool:
        root = _m2_output_directory(output)
        if {path.name for path in root.iterdir()} != _OUTPUT_FILES:
            raise ExperimentalM2ProxyError("existing M2 training output set is not exact")
        for name, payload in sorted(payloads.items()):
            if _m2_regular_file(root / name).read_bytes() != payload:
                raise ExperimentalM2ProxyError(f"existing M2 training output differs: {name}")
        return True

    if output.exists():
        return verify_existing()
    output.parent.mkdir(parents=True, exist_ok=True)
    _m2_reject_symlinks(output.parent, allow_missing=False)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    try:
        for name, payload in sorted(payloads.items()):
            descriptor = os.open(temporary / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        try:
            os.rename(temporary, output)
        except OSError as exc:
            if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                raise
            if temporary.exists():
                shutil.rmtree(temporary)
            return verify_existing()
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return False


def _m2_reject_symlinks(path: Path, *, allow_missing: bool) -> Path:
    try:
        return _reject_symlinks(path, allow_missing=allow_missing)
    except ExperimentalM0ProxyError as exc:
        raise ExperimentalM2ProxyError(str(exc)) from exc


def _m2_output_directory(path: Path) -> Path:
    """Return one real output directory while rejecting every symlink component."""

    absolute = _m2_reject_symlinks(path, allow_missing=False)
    try:
        mode = absolute.lstat().st_mode
    except OSError as exc:
        raise ExperimentalM2ProxyError(f"M2 output directory is unavailable: {absolute}") from exc
    if not stat.S_ISDIR(mode):
        raise ExperimentalM2ProxyError(f"M2 output is not a directory: {absolute}")
    return absolute


def _m2_regular_file(path: Path) -> Path:
    """Reject symlinked or non-regular M2 output members without following them."""

    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise ExperimentalM2ProxyError(f"required M2 output is unavailable: {path}") from exc
    if not stat.S_ISREG(mode):
        raise ExperimentalM2ProxyError(f"required M2 output is not a regular file: {path}")
    return path


def _m2_strict_json(path: Path) -> object:
    try:
        return json.loads(_m2_regular_file(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ExperimentalM2ProxyError(f"invalid M2 output JSON: {path}") from exc


def _train_loaded_m2_proxy_one_epoch(
    *,
    repository_root: Path,
    prepared_input_dir: Path,
    output_dir: Path,
    runtime: LoadedM2ProxyRuntime,
    allow_experimental_mixed_supervision: bool,
    device: str,
    module_importer: ModuleImporter,
) -> M2ProxyTrainingArtifacts:
    if not allow_experimental_mixed_supervision:
        raise ExperimentalM2ProxyError("M2 training requires explicit experimental opt-in")
    repository = _real_directory(repository_root)
    prepared = _real_directory(prepared_input_dir)
    output = _m2_reject_symlinks(output_dir, allow_missing=True)
    if any(_paths_overlap(output, protected) for protected in (repository, prepared)):
        raise ExperimentalM2ProxyError("M2 output must be disjoint from code and inputs")
    input_manifest = cast(
        Any, _validate_protocol_against_input(runtime.protocol, prepared_input_dir=prepared)
    )
    _verify_audited_tokenizer_snapshot(runtime.audited_tokenizer_snapshot)
    verify_local_modernbert_checkpoint(runtime.checkpoint)
    examples = _load_prepared_examples(prepared)
    trusted_tokenizer = _load_audited_tokenizer(runtime.audited_tokenizer_snapshot)
    trusted_tokenization = _prepared_tokenization_sha256(trusted_tokenizer, examples)
    runtime_tokenization = _prepared_tokenization_sha256(runtime.tokenizer, examples)
    if (
        trusted_tokenization != runtime.prepared_tokenization_sha256
        or runtime_tokenization != runtime.prepared_tokenization_sha256
    ):
        raise ExperimentalM2ProxyError("M2 runtime tokenizer differs from audited tokenizer")
    train_examples = tuple(item for item in examples if item.proxy_training_eligible)
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
        raise ExperimentalM2ProxyError("M2 training requires torch") from exc
    model = cast(Any, runtime.model)
    if not isinstance(model, torch.nn.Module):
        raise ExperimentalM2ProxyError("M2 training model is not a torch module")
    initial_state = _safetensors_state_bytes(model, module_importer=module_importer)
    initial_hash = hashlib.sha256(initial_state).hexdigest()
    if initial_hash != runtime.initial_model_state_sha256:
        raise ExperimentalM2ProxyError("M2 model changed after exact checkpoint loading")
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
            raise ExperimentalM2ProxyError("M2 encountered an incomplete effective batch")
        optimizer.zero_grad(set_to_none=True)
        logical_batch_loss = 0.0
        microbatch_count = 0
        for micro_start in range(0, batch_size, microbatch_size):
            micro_items = items[micro_start : micro_start + microbatch_size]
            micro_rows = rows[micro_start : micro_start + microbatch_size]
            batch = _tensorize_m0_batch(
                tokenizer=trusted_tokenizer,
                examples=micro_items,
                selected_length=runtime.selected_length,
                weights=[cast(float, item.loss_weight) for item in micro_rows],
                device=device,
                module_importer=module_importer,
            )
            result = model(
                source_input_ids=batch["source_input_ids"],
                source_attention_mask=batch["source_attention_mask"],
                candidate_input_ids=batch["candidate_input_ids"],
                candidate_attention_mask=batch["candidate_attention_mask"],
            )
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
            raise ExperimentalM2ProxyError("M2 gradient accumulation differs from protocol")
        optimizer.step()
        weighted_loss_sum += logical_batch_loss
        step_count += 1
    if step_count != schedule.batch_count:
        raise ExperimentalM2ProxyError("M2 optimizer-step count differs from schedule")
    final_state = _safetensors_state_bytes(model, module_importer=module_importer)
    final_hash = hashlib.sha256(final_state).hexdigest()
    trusted_runtime = LoadedM2ProxyRuntime(
        model=model,
        tokenizer=trusted_tokenizer,
        selected_length=runtime.selected_length,
        checkpoint=runtime.checkpoint,
        audited_tokenizer_snapshot=runtime.audited_tokenizer_snapshot,
        protocol=runtime.protocol,
        initial_model_state_sha256=runtime.initial_model_state_sha256,
        prepared_tokenization_sha256=runtime.prepared_tokenization_sha256,
    )
    diagnostic_examples = {
        "train": selected_examples,
        "validation": tuple(
            item for item in examples if item.split == "validation" and not item.long_input
        ),
        "test": tuple(item for item in examples if item.split == "test" and not item.long_input),
    }
    predictions_by_split = {
        split: _predict_m2_examples(
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
    swap_check = _check_swap_invariance(
        runtime=trusted_runtime,
        examples=tuple(item for split in _SPLITS for item in diagnostic_examples[split]),
        device=device,
        module_importer=module_importer,
    )
    metrics = M0ProxyTrainingMetrics(
        optimizer_steps=step_count,
        examples_exposed=len(selected_examples),
        selected_component_count=schedule.selected_component_count,
        loss_normalization_weight=schedule.loss_normalization_weight,
        mean_weighted_training_bce=weighted_loss_sum / step_count,
        initial_state_sha256=initial_hash,
        final_state_sha256=final_hash,
        diagnostics={split: _m2_metric_set(predictions_by_split[split]) for split in _SPLITS},
    )
    non_manifest = {
        "epoch_schedule.json": canonical_json_bytes(schedule.model_dump(mode="json")) + b"\n",
        "metrics.json": canonical_json_bytes(metrics.model_dump(mode="json")) + b"\n",
        "model.safetensors": final_state,
        "predictions.jsonl": _canonical_jsonl(predictions),
        "swap_invariance.json": canonical_json_bytes(swap_check.model_dump(mode="json")) + b"\n",
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
        "artifact_kind": "experimental_m2_proxy_training_v1",
        "prepared_input_artifact_id": input_manifest.artifact_id,
        "prepared_input_manifest_sha256": hash_file(prepared / "manifest.json"),
        "dataset_id": input_manifest.dataset_id,
        "protocol_hash": hash_canonical(runtime.protocol.model_dump(mode="json")),
        "protocol": runtime.protocol.model_dump(mode="json"),
        "code": code.model_dump(mode="json"),
        "pretrained_checkpoint": runtime.checkpoint.receipt.model_dump(mode="json"),
        "tokenizer_audit_id": input_manifest.tokenizer_decision.audit_id,
        "tokenizer_snapshot_content_hash": input_manifest.tokenizer_decision.snapshot_content_hash,
        "prepared_tokenization_sha256": trusted_tokenization,
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
        "swap_invariance": swap_check.model_dump(mode="json"),
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
    manifest = M2ProxyTrainingManifest.model_validate(
        {
            **manifest_data,
            "artifact_id": "experimental-m2-proxy-training:" + hash_canonical(manifest_data),
        }
    )
    payloads = {
        **non_manifest,
        "manifest.json": canonical_json_bytes(manifest.model_dump(mode="json")) + b"\n",
    }
    replayed = _write_or_replay_m2_training(output, payloads)
    verify_m2_proxy_training(output)
    return M2ProxyTrainingArtifacts(
        output_dir=output,
        artifact_id=manifest.artifact_id,
        optimizer_steps=step_count,
        examples_exposed=len(selected_examples),
        replayed=replayed,
    )


def verify_m2_proxy_training(
    output_dir: Path,
    *,
    repository_root: Path | None = None,
    prepared_input_dir: Path | None = None,
    checkpoint: M0LocalCheckpointBinding | None = None,
    audited_tokenizer_snapshot: SnapshotBinding | None = None,
    protocol: ExperimentalM2ProxyProtocolConfig | None = None,
) -> M2ProxyTrainingManifest:
    """Verify M2 bytes, schedule, diagnostics, and optional exact bindings."""

    root = _m2_output_directory(output_dir)
    if {path.name for path in root.iterdir()} != _OUTPUT_FILES:
        raise ExperimentalM2ProxyError("M2 training output file set is not exact")
    manifest = M2ProxyTrainingManifest.model_validate(_m2_strict_json(root / "manifest.json"))
    for name, expected in manifest.output_sha256.items():
        if hash_file(_m2_regular_file(root / name)) != expected:
            raise ExperimentalM2ProxyError(f"M2 training output hash differs: {name}")
    schedule = M0EpochSchedule.model_validate(_m2_strict_json(root / "epoch_schedule.json"))
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
        raise ExperimentalM2ProxyError("M2 schedule differs from manifest")
    predictions: list[M2ProxyPrediction] = []
    with _m2_regular_file(root / "predictions.jsonl").open("rb") as handle:
        for line_number, raw in enumerate(handle, start=1):
            try:
                item = M2ProxyPrediction.model_validate(json.loads(raw))
            except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
                raise ExperimentalM2ProxyError(
                    f"invalid M2 prediction at line {line_number}"
                ) from exc
            if raw != canonical_json_bytes(item.model_dump(mode="json")) + b"\n":
                raise ExperimentalM2ProxyError(f"noncanonical M2 prediction at line {line_number}")
            predictions.append(item)
    if len({item.record_id for item in predictions}) != len(predictions):
        raise ExperimentalM2ProxyError("M2 predictions repeat a record")
    if predictions != sorted(predictions, key=lambda item: item.record_id):
        raise ExperimentalM2ProxyError("M2 predictions are not record-ID sorted")
    selected_schedule_rows = tuple(
        sorted(
            (item for item in schedule.records if item.selection_status == "selected"),
            key=lambda item: item.record_id,
        )
    )
    observed_train = tuple(
        sorted(
            (item.record_id, item.split, item.pseudo_target)
            for item in predictions
            if item.split == "train"
        )
    )
    expected_train = tuple(
        (item.record_id, "train", item.pseudo_target) for item in selected_schedule_rows
    )
    if observed_train != expected_train:
        raise ExperimentalM2ProxyError("M2 train predictions differ from selected schedule")
    metrics = M0ProxyTrainingMetrics.model_validate(_m2_strict_json(root / "metrics.json"))
    grouped = {
        split: tuple(item for item in predictions if item.split == split) for split in _SPLITS
    }
    if metrics.diagnostics != {split: _m2_metric_set(grouped[split]) for split in _SPLITS}:
        raise ExperimentalM2ProxyError("M2 metrics differ from predictions")
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
        raise ExperimentalM2ProxyError("M2 metrics differ from manifest")
    swap = M2SwapInvarianceCheck.model_validate(_m2_strict_json(root / "swap_invariance.json"))
    if swap != manifest.swap_invariance:
        raise ExperimentalM2ProxyError("M2 swap check differs from manifest")
    try:
        safetensors_torch = cast(Any, importlib.import_module("safetensors.torch"))
        state = safetensors_torch.load(_m2_regular_file(root / "model.safetensors").read_bytes())
    except Exception as exc:
        raise ExperimentalM2ProxyError("M2 output is not a valid safetensors checkpoint") from exc
    if not isinstance(state, dict) or not state:
        raise ExperimentalM2ProxyError("M2 safetensors checkpoint has no state")
    for name in (
        "manifest.json",
        "metrics.json",
        "epoch_schedule.json",
        "predictions.jsonl",
        "swap_invariance.json",
    ):
        if b"[HEADLESS]\n" in _m2_regular_file(root / name).read_bytes():
            raise ExperimentalM2ProxyError("M2 training metadata leaks model-visible text")
    if (
        repository_root is not None
        and collect_code_state(_real_directory(repository_root)) != manifest.code
    ):
        raise ExperimentalM2ProxyError("current code differs from M2 training freeze")
    external = (prepared_input_dir, checkpoint, audited_tokenizer_snapshot, protocol)
    if any(value is not None for value in external):
        if any(value is None for value in external):
            raise ExperimentalM2ProxyError(
                "exact M2 verification requires inputs, checkpoint, tokenizer, and protocol"
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
            or prepared_manifest.tokenizer_decision.audit_id != manifest.tokenizer_audit_id
        ):
            raise ExperimentalM2ProxyError("prepared M2 dependencies differ from manifest")
        _verify_audited_tokenizer_snapshot(audited_tokenizer_snapshot)
        if (
            audited_tokenizer_snapshot.snapshot_content_hash
            != manifest.tokenizer_snapshot_content_hash
            or audited_tokenizer_snapshot.model_id != manifest.pretrained_checkpoint.model_id
            or audited_tokenizer_snapshot.revision != manifest.pretrained_checkpoint.revision
            or checkpoint.receipt != manifest.pretrained_checkpoint
        ):
            raise ExperimentalM2ProxyError("M2 tokenizer/checkpoint differs from manifest")
        verify_local_modernbert_checkpoint(checkpoint)
        runtime = load_m2_proxy_runtime(
            prepared_input_dir=prepared,
            checkpoint=checkpoint,
            audited_tokenizer_snapshot=audited_tokenizer_snapshot,
            protocol=protocol,
            allow_experimental_mixed_supervision=True,
        )
        if runtime.initial_model_state_sha256 != manifest.initial_model_state_sha256:
            raise ExperimentalM2ProxyError(
                "M2 initial model state differs from exact checkpoint architecture"
            )
        examples = _load_prepared_examples(prepared)
        if _prepared_tokenization_sha256(runtime.tokenizer, examples) != (
            manifest.prepared_tokenization_sha256
        ):
            raise ExperimentalM2ProxyError("M2 prepared tokenization differs")
        train_examples = tuple(item for item in examples if item.proxy_training_eligible)
        rebuilt = build_m0_epoch_schedule(
            train_examples,
            batch_size=protocol.training.batch_size,
            seed=protocol.training.seed,
            max_unique_variants_per_ancestry=protocol.training.max_unique_variants_per_ancestry,
        )
        if rebuilt != schedule:
            raise ExperimentalM2ProxyError("M2 schedule does not replay from exact inputs")
        selected_ids = {
            item.record_id for item in rebuilt.records if item.selection_status == "selected"
        }
        expected_prediction_bindings = tuple(
            sorted(
                (
                    item.record_id,
                    item.split,
                    item.pseudo_target,
                    item.private_source_content,
                )
                for item in examples
                if (
                    (item.split == "train" and item.record_id in selected_ids)
                    or (item.split in {"validation", "test"} and not item.long_input)
                )
            )
        )
        observed_prediction_bindings = tuple(
            sorted(
                (
                    item.record_id,
                    item.split,
                    item.pseudo_target,
                    item.private_source_content,
                )
                for item in predictions
            )
        )
        if observed_prediction_bindings != expected_prediction_bindings:
            raise ExperimentalM2ProxyError(
                "M2 predictions differ from exact prepared examples and schedule"
            )
        model = cast(Any, runtime.model)
        model.load_state_dict(state, strict=True)
        replay_device = manifest.runtime.device
        model.to(
            device=replay_device,
            dtype=cast(Any, importlib.import_module("torch")).float32,
        )
        final_runtime = LoadedM2ProxyRuntime(
            model=model,
            tokenizer=runtime.tokenizer,
            selected_length=runtime.selected_length,
            checkpoint=runtime.checkpoint,
            audited_tokenizer_snapshot=runtime.audited_tokenizer_snapshot,
            protocol=runtime.protocol,
            initial_model_state_sha256=runtime.initial_model_state_sha256,
            prepared_tokenization_sha256=runtime.prepared_tokenization_sha256,
        )
        exact_examples = tuple(
            item
            for item in examples
            if (
                (item.split == "train" and item.record_id in selected_ids)
                or (item.split in {"validation", "test"} and not item.long_input)
            )
        )
        replayed_predictions = _predict_m2_examples(
            runtime=final_runtime,
            examples=exact_examples,
            batch_size=protocol.training.microbatch_size,
            device=replay_device,
            module_importer=importlib.import_module,
        )
        _require_prediction_replay(
            predictions,
            replayed_predictions,
            absolute_tolerance=protocol.architecture.prediction_replay_atol,
        )
        _require_replayed_diagnostics(
            metrics.diagnostics,
            replayed_predictions,
            absolute_tolerance=protocol.architecture.prediction_replay_atol,
        )
        rebuilt_swap = _check_swap_invariance(
            runtime=final_runtime,
            examples=exact_examples,
            device=replay_device,
            module_importer=importlib.import_module,
        )
        if rebuilt_swap != manifest.swap_invariance:
            raise ExperimentalM2ProxyError("M2 swap check does not replay from checkpoint")
    return manifest


__all__ = [
    "ExperimentalM2ProxyError",
    "ExperimentalM2ProxyProtocolConfig",
    "LoadedM2ProxyRuntime",
    "M2ProxyArchitectureProtocol",
    "M2ProxyTrainingArtifacts",
    "M2ProxyTrainingManifest",
    "M2SwapInvarianceCheck",
    "build_m2_bidirectional_matcher_module",
    "load_experimental_m2_proxy_config",
    "load_m2_proxy_runtime",
    "train_m2_proxy_one_epoch",
    "verify_m2_proxy_training",
]
