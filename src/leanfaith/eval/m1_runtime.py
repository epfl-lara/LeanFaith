"""Minimal runtime for scoring statement pairs with the trained M1 model."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast

import safetensors.torch
import torch
from transformers import AutoConfig, AutoModel, AutoTokenizer

from leanfaith.models.m0_dual_encoder import _HEADLESS_MARKER
from leanfaith.models.m1_cross_encoder import (
    _CANDIDATE_TAG,
    _REFERENCE_TAG,
    build_m1_cross_encoder_module,
)


class _Tokenizer(Protocol):
    def encode(self, text: str, *, add_special_tokens: bool) -> list[int]: ...

    def __call__(
        self,
        texts: Sequence[str],
        *,
        padding: bool,
        truncation: bool,
        max_length: int,
        return_tensors: Literal["pt"],
    ) -> Mapping[str, torch.Tensor]: ...


@dataclass(frozen=True, slots=True)
class M1Scorer:
    """Loaded M1 model, tokenizer, and execution device."""

    model: torch.nn.Module
    tokenizer: _Tokenizer
    device: torch.device


@dataclass(frozen=True, slots=True)
class PairScore:
    """One probability or an explicit overlength abstention."""

    probability: float | None
    token_length: int
    abstained: bool


def pack_pair(reference_headless: str, candidate_headless: str) -> str:
    """Pack raw headless views byte-for-byte like the M0-to-M1 training path."""

    return (
        _REFERENCE_TAG
        + _HEADLESS_MARKER
        + reference_headless
        + _CANDIDATE_TAG
        + _HEADLESS_MARKER
        + candidate_headless
    )


def load_m1_scorer(checkpoint_path: Path, snapshot_dir: Path, device: str) -> M1Scorer:
    """Rebuild M1 from local config bytes and load its complete state dict."""

    tokenizer = cast(
        _Tokenizer,
        AutoTokenizer.from_pretrained(  # type: ignore[no-untyped-call]
            snapshot_dir,
            local_files_only=True,
            trust_remote_code=False,
        ),
    )
    config = AutoConfig.from_pretrained(snapshot_dir, local_files_only=True)
    encoder = AutoModel.from_config(config)  # type: ignore[no-untyped-call]
    model = cast(
        torch.nn.Module,
        build_m1_cross_encoder_module(
            encoder=encoder,
            hidden_size=int(encoder.config.hidden_size),
        ),
    )
    state = safetensors.torch.load_file(checkpoint_path)
    model.load_state_dict(state, strict=True)
    target_device = torch.device(device)
    model.to(target_device)
    model.eval()
    return M1Scorer(model=model, tokenizer=tokenizer, device=target_device)


def _token_lengths(tokenizer: _Tokenizer, texts: Sequence[str]) -> list[int]:
    lengths: list[int] = []
    for text in texts:
        token_ids = tokenizer.encode(text, add_special_tokens=True)
        if not token_ids:
            raise ValueError("M1 tokenizer returned an empty sequence")
        lengths.append(len(token_ids))
    return lengths


def _score_selected(
    scorer: M1Scorer,
    texts: Sequence[str],
    selected_indices: Sequence[int],
    *,
    batch_size: int,
    max_length: int,
) -> dict[int, float]:
    probabilities: dict[int, float] = {}
    for start in range(0, len(selected_indices), batch_size):
        batch_indices = selected_indices[start : start + batch_size]
        batch_texts = [texts[index] for index in batch_indices]
        packed = scorer.tokenizer(
            batch_texts,
            padding=True,
            truncation=True,
            max_length=max_length,
            return_tensors="pt",
        )
        if not {"input_ids", "attention_mask"}.issubset(packed):
            raise ValueError("M1 tokenizer output lacks input_ids or attention_mask")
        input_ids = packed["input_ids"]
        attention_mask = packed["attention_mask"]
        if (
            input_ids.ndim != 2
            or attention_mask.ndim != 2
            or input_ids.shape != attention_mask.shape
            or input_ids.shape[0] != len(batch_indices)
            or input_ids.shape[1] > max_length
        ):
            raise ValueError("M1 tokenizer returned an incompatible batch")
        with torch.no_grad():
            raw_output = scorer.model(
                input_ids=input_ids.to(scorer.device),
                attention_mask=attention_mask.to(scorer.device),
            )
        output = cast(Mapping[str, torch.Tensor], raw_output)
        values = output.get("probabilities")
        if values is None or values.ndim != 1 or values.shape[0] != len(batch_indices):
            raise ValueError("M1 model returned incompatible probabilities")
        batch_probabilities = cast(list[float], values.detach().cpu().tolist())
        for index, probability in zip(batch_indices, batch_probabilities, strict=True):
            value = float(probability)
            if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                raise ValueError("M1 model returned an invalid probability")
            probabilities[index] = value
    return probabilities


def _validate_scoring_arguments(*, batch_size: int, max_length: int) -> None:
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    if max_length <= 0:
        raise ValueError("max_length must be positive")


def score_pairs(
    scorer: M1Scorer,
    pairs: Sequence[tuple[str, str]],
    batch_size: int,
    max_length: int = 1024,
) -> list[PairScore]:
    """Score in-range pairs and explicitly abstain on every overlength pair."""

    _validate_scoring_arguments(batch_size=batch_size, max_length=max_length)
    texts = [pack_pair(reference, candidate) for reference, candidate in pairs]
    lengths = _token_lengths(scorer.tokenizer, texts)
    selected = [index for index, length in enumerate(lengths) if length <= max_length]
    probabilities = _score_selected(
        scorer,
        texts,
        selected,
        batch_size=batch_size,
        max_length=max_length,
    )
    return [
        PairScore(
            probability=probabilities.get(index),
            token_length=length,
            abstained=index not in probabilities,
        )
        for index, length in enumerate(lengths)
    ]


def score_pairs_truncated(
    scorer: M1Scorer,
    pairs: Sequence[tuple[str, str]],
    batch_size: int,
    max_length: int = 1024,
) -> list[PairScore]:
    """Secondary metric only: score every pair after explicit tokenizer truncation."""

    _validate_scoring_arguments(batch_size=batch_size, max_length=max_length)
    texts = [pack_pair(reference, candidate) for reference, candidate in pairs]
    lengths = _token_lengths(scorer.tokenizer, texts)
    selected = list(range(len(texts)))
    probabilities = _score_selected(
        scorer,
        texts,
        selected,
        batch_size=batch_size,
        max_length=max_length,
    )
    return [
        PairScore(
            probability=probabilities[index],
            token_length=length,
            abstained=False,
        )
        for index, length in enumerate(lengths)
    ]


__all__ = [
    "M1Scorer",
    "PairScore",
    "load_m1_scorer",
    "pack_pair",
    "score_pairs",
    "score_pairs_truncated",
]
