"""Blinding primitives for independent human annotation exports.

Only the small, explicitly allow-listed annotation projection is public.  The
source frame, generator lineage, sampling design, and opaque-ID linkage stay in
the private export namespace.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable, Mapping, Sequence

_MIN_ENTROPY_BYTES = 32
_BLIND_ID_DOMAIN = b"leanfaith-lf021-prevalence-annotation-item-token-v1\0"
_ORDER_DOMAIN = b"leanfaith-lf021-prevalence-annotation-order-v1\0"

_FORBIDDEN_VISIBLE_KEYS = frozenset(
    {
        "decision",
        "evidence_ids",
        "family_id",
        "frame_record_id",
        "gate_5_closed",
        "gate_5g_credit_claimed",
        "generator_id",
        "inclusion_probability_denominator",
        "inclusion_probability_numerator",
        "intended_relation",
        "invocation_id",
        "label",
        "labels",
        "member_count",
        "member_count_by_family",
        "member_count_by_pool",
        "member_count_by_source_proxy",
        "model_score",
        "pair_id",
        "pool_id",
        "population_record_id",
        "prior_vote",
        "prior_votes",
        "problem_group",
        "problem_record_id",
        "quality_tier",
        "relation",
        "requires_adjudication",
        "resolution_outcome",
        "same_claim",
        "sampling_rank_digest",
        "sampling_seed_sha256",
        "sampling_stratum",
        "score",
        "scores",
        "seed",
        "source_proxy",
        "split_group_ids",
        "stratum_population_size",
        "stratum_sample_size",
        "supervision_eligible",
        "theorem_a_id",
        "theorem_b_id",
        "vote",
        "votes",
    }
)

_GENERATOR_MARKERS = (
    "autoformalizer",
    "goedel_formalizer",
    "kimina_autoformalizer",
    "lf021_research_",
    "stepfun_formalizer",
)


class BlindingError(ValueError):
    """Raised when an annotation artifact could expose forbidden context."""


def validate_entropy(entropy: bytes) -> None:
    """Require independently supplied high-entropy randomization material."""

    if len(entropy) < _MIN_ENTROPY_BYTES:
        raise BlindingError(f"annotation randomization needs at least {_MIN_ENTROPY_BYTES} bytes")


def blind_item_id(*, entropy: bytes, annotator_slot: str, frame_record_id: str) -> str:
    """Derive an opaque bundle-local item identifier without persisting entropy."""

    validate_entropy(entropy)
    if not annotator_slot or not frame_record_id:
        raise BlindingError("annotator_slot and frame_record_id must be nonempty")
    message = annotator_slot.encode("utf-8") + b"\0" + frame_record_id.encode("utf-8")
    digest = hmac.new(entropy, _BLIND_ID_DOMAIN + message, hashlib.sha256).hexdigest()
    return f"lf023_blind_item_v1:{digest}"


def independently_randomized[T](
    values: Sequence[T],
    *,
    entropy: bytes,
    annotator_slot: str,
    stable_key: Callable[[T], str],
) -> tuple[T, ...]:
    """Return a deterministic CSPRNG-keyed permutation for one annotator.

    The key is intentionally not returned.  The immutable output and private
    linkage sidecar are the audit records; annotators never receive the
    randomization material or source order.
    """

    validate_entropy(entropy)
    if not annotator_slot:
        raise BlindingError("annotator_slot must be nonempty")
    ranked: list[tuple[bytes, str, T]] = []
    for value in values:
        key = stable_key(value)
        if not key:
            raise BlindingError("randomized values require a nonempty stable key")
        message = annotator_slot.encode("utf-8") + b"\0" + key.encode("utf-8")
        rank = hmac.new(entropy, _ORDER_DOMAIN + message, hashlib.sha256).digest()
        ranked.append((rank, key, value))
    if len({key for _, key, _ in ranked}) != len(ranked):
        raise BlindingError("randomized values contain duplicate stable keys")
    ranked.sort(key=lambda item: (item[0], item[1]))
    return tuple(value for _, _, value in ranked)


def assert_blinded_payload(value: object, *, path: str = "$") -> None:
    """Fail closed when a public payload contains lineage or outcome fields."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise BlindingError(f"non-string public key at {path}")
            normalized = key.casefold()
            if normalized in _FORBIDDEN_VISIBLE_KEYS:
                raise BlindingError(f"forbidden annotation key {key!r} at {path}")
            assert_blinded_payload(item, path=f"{path}.{key}")
        return
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        for index, item in enumerate(value):
            assert_blinded_payload(item, path=f"{path}[{index}]")


def assert_name_free_statement(statement: str, *, field_name: str) -> None:
    """Reject known autoformalizer/declaration markers from displayed views."""

    if not statement.strip():
        raise BlindingError(f"{field_name} must be nonempty")
    lowered = statement.casefold()
    leaked = sorted(marker for marker in _GENERATOR_MARKERS if marker in lowered)
    if leaked:
        raise BlindingError(f"{field_name} exposes generator/declaration markers: {leaked}")
