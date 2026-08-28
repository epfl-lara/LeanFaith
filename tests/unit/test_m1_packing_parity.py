from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Literal

import pytest
import torch

from leanfaith.eval.m1_runtime import M1Scorer, pack_pair, score_pairs
from leanfaith.models.m0_dual_encoder import _HEADLESS_MARKER, M0ProxyExample
from leanfaith.models.m1_cross_encoder import pack_m1_pair


def _training_example(index: int, reference: str, candidate: str) -> M0ProxyExample:
    source_text = _HEADLESS_MARKER + reference
    candidate_text = _HEADLESS_MARKER + candidate
    return M0ProxyExample(
        record_id=f"experimental_mixed_pair:{index:064x}",
        split_component_id=f"split-component:{index:064x}",
        split="train",
        pseudo_target="same_claim",
        source_text=source_text,
        candidate_text=candidate_text,
        source_text_sha256=hashlib.sha256(source_text.encode()).hexdigest(),
        candidate_text_sha256=hashlib.sha256(candidate_text.encode()).hexdigest(),
        source_token_count=1,
        candidate_token_count=1,
        selected_length=512,
        long_input=False,
        proxy_training_eligible=True,
        private_source_content=False,
        redistribution_allowed=True,
        external_transmission_allowed=True,
        release_eligible=True,
    )


@pytest.mark.parametrize(
    ("reference", "candidate"),
    [
        ("theorem r : True := by trivial", "theorem c : True := by trivial"),
        ("(n : Nat) : n + 0 = n", "(m : Nat) : 0 + m = m"),
        ("(α : Type) (x : α) : x = x", "(β : Type) (y : β) : y = y"),
        ("∀ ε > 0, ∃ δ > 0, δ ≤ ε", "∀ η > 0, ∃ κ > 0, κ ≤ η"),
        ("line one\nline two ∧ x ≠ y", "candidate\nsecond line ⊢ x = x"),
    ],
)
def test_pack_pair_matches_real_training_packer(reference: str, candidate: str) -> None:
    example = _training_example(1, reference, candidate)

    assert pack_pair(reference, candidate).encode() == pack_m1_pair(example).encode()


class _CharacterTokenizer:
    def encode(self, text: str, *, add_special_tokens: bool) -> list[int]:
        values = [ord(character) for character in text]
        return [1, *values, 2] if add_special_tokens else values

    def __call__(
        self,
        texts: Sequence[str],
        *,
        padding: bool,
        truncation: bool,
        max_length: int,
        return_tensors: Literal["pt"],
    ) -> dict[str, torch.Tensor]:
        raise AssertionError("an abstained pair must not be tensorized")


class _NeverCalledModel(torch.nn.Module):
    def forward(self, *, input_ids: torch.Tensor, attention_mask: torch.Tensor) -> object:
        raise AssertionError("an abstained pair must not reach the model")


def test_overlength_pair_abstains_without_truncation() -> None:
    scorer = M1Scorer(
        model=_NeverCalledModel(),
        tokenizer=_CharacterTokenizer(),
        device=torch.device("cpu"),
    )

    scores = score_pairs(scorer, [("x" * 100, "y" * 100)], batch_size=1, max_length=8)

    assert len(scores) == 1
    assert scores[0].probability is None
    assert scores[0].abstained is True
    assert scores[0].token_length > 8
