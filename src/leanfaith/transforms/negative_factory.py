"""Static, fail-closed construction of code-owned unary LF-018 negative rules.

The normal :class:`~leanfaith.transforms.registry.TransformationRegistry` is
deliberately unary.  This factory registers only N01, N02, N03, and N07 in
that runtime.  N10 is constructed through the same code-owned, hash-bound
factory but returned separately for the dedicated two-source dispatcher; it
is never smuggled into the unary runtime by weakening lineage checks.

YAML may select only implementation keys compiled into this module.  It cannot
name import paths or dynamically load code.  Every constructed rule is bound
to the effective transformation-registry hash and checked against the
configured identity before registration.  The resulting drafts remain
provisional mutation provenance and are never resolved labels.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from leanfaith.config.paths import find_repo_root
from leanfaith.schemas.enums import Polarity
from leanfaith.transforms.n01_operator import N01OperatorRule
from leanfaith.transforms.negatives.n02_quantifier import N02QuantifierRule
from leanfaith.transforms.negatives.n03_drop_hypothesis import N03DropHypothesisRule
from leanfaith.transforms.negatives.n07_literal_bound import N07LiteralBoundRule
from leanfaith.transforms.negatives.n10_nearby_theorem import N10NearbyTheoremRule
from leanfaith.transforms.protocol import PairTransformationRule, TransformationRule
from leanfaith.transforms.registry import (
    LoadedTransformationRegistry,
    RejectionSink,
    RuleImplementationStatus,
    TransformationRegistry,
    TransformationRuleConfig,
)


class NegativeRuleFactoryError(ValueError):
    """A configured unary negative implementation cannot be safely constructed."""


UnaryRuleBuilder = Callable[
    [LoadedTransformationRegistry, Path],
    TransformationRule,
]
PairRuleBuilder = Callable[
    [LoadedTransformationRegistry, Path],
    PairTransformationRule,
]


def _build_n01(
    loaded: LoadedTransformationRegistry,
    repo_root: Path,
) -> TransformationRule:
    return N01OperatorRule.from_repository(
        generation_config_hash=loaded.registry_hash,
        repo_root=repo_root,
    )


def _build_n02(
    loaded: LoadedTransformationRegistry,
    repo_root: Path,
) -> TransformationRule:
    return N02QuantifierRule.from_repository(
        registry_hash=loaded.registry_hash,
        repo_root=repo_root,
    )


def _build_n03(
    loaded: LoadedTransformationRegistry,
    repo_root: Path,
) -> TransformationRule:
    return N03DropHypothesisRule.from_repository(
        registry_hash=loaded.registry_hash,
        repo_root=repo_root,
    )


def _build_n07(
    loaded: LoadedTransformationRegistry,
    repo_root: Path,
) -> TransformationRule:
    return N07LiteralBoundRule.from_repository(
        registry_hash=loaded.registry_hash,
        repo_root=repo_root,
    )


def _build_n10(
    loaded: LoadedTransformationRegistry,
    repo_root: Path,
) -> PairTransformationRule:
    return N10NearbyTheoremRule.from_repository(
        generation_config_hash=loaded.registry_hash,
        repo_root=repo_root,
    )


# This is the complete code-owned unary implementation boundary.  Never
# populate it from YAML, entry points, module paths, or user-provided strings.
_CODE_OWNED_UNARY_NEGATIVE_BUILDERS: Mapping[str, UnaryRuleBuilder] = MappingProxyType(
    {
        "n01_operator": _build_n01,
        "n02_quantifier": _build_n02,
        "n03_drop_hypothesis": _build_n03,
        "n07_literal_bound": _build_n07,
    }
)

# This is a separate code-owned boundary because pair rules must never enter
# the unary registry.
_CODE_OWNED_PAIR_NEGATIVE_BUILDERS: Mapping[str, PairRuleBuilder] = MappingProxyType(
    {
        "n10_nearby_theorem": _build_n10,
    }
)

# This full identity, not merely a YAML-selected implementation key, identifies
# N10 as pair-aware.  Another rule cannot evade unary validation by borrowing
# the N10 key.
_PAIR_AWARE_NEGATIVE_IDENTITY = (
    "n10_nearby_theorem",
    "n10_nearby_theorem",
    "n10_nearby_theorem",
)


@dataclass(frozen=True, slots=True)
class NegativeRuleRegistration:
    """Guarded unary runtime plus separately constructed pair implementations."""

    runtime: TransformationRegistry
    registered_rule_ids: tuple[str, ...]
    skipped_rule_ids: tuple[str, ...]
    pair_aware_rule_ids: tuple[str, ...]
    pair_rules: tuple[PairTransformationRule, ...]
    registry_hash: str


def _configured_negative_rules(
    loaded: LoadedTransformationRegistry,
) -> tuple[TransformationRuleConfig, ...]:
    return tuple(
        rule
        for family in loaded.config.families
        if family.polarity == Polarity.NEGATIVE
        for rule in family.rules
    )


def _is_pair_aware(rule: TransformationRuleConfig) -> bool:
    return (rule.rule_id, rule.family_id, rule.implementation_key) == (
        _PAIR_AWARE_NEGATIVE_IDENTITY
    )


def _validate_implementation_identity(
    implementation: TransformationRule | PairTransformationRule,
    configured: TransformationRuleConfig,
    effective_registry_hash: str,
) -> None:
    mismatches = tuple(
        field_name
        for field_name, expected in (
            ("rule_id", configured.rule_id),
            ("rule_version", configured.rule_version),
            ("family_id", configured.family_id),
            ("polarity", configured.polarity),
            ("implementation_key", configured.implementation_key),
        )
        if getattr(implementation, field_name, None) != expected
    )
    if mismatches:
        raise NegativeRuleFactoryError(
            f"implementation {configured.implementation_key!r} metadata mismatch: "
            f"{','.join(mismatches)}"
        )
    bound_hashes = tuple(
        value
        for field_name in ("generation_config_hash", "registry_hash")
        if isinstance((value := getattr(implementation, field_name, None)), str)
    )
    if not bound_hashes:
        raise NegativeRuleFactoryError(
            f"implementation {configured.implementation_key!r} exposes no registry-hash binding"
        )
    if any(value != effective_registry_hash for value in bound_hashes):
        raise NegativeRuleFactoryError(
            f"implementation {configured.implementation_key!r} is not bound to "
            "the effective registry hash"
        )


def build_negative_rule_runtime(
    loaded: LoadedTransformationRegistry,
    *,
    rejection_sink: RejectionSink | None = None,
) -> NegativeRuleRegistration:
    """Construct every available code-owned unary negative rule.

    Pending and disabled unary rules are recorded as skipped.  Available
    pair-aware N10 rules are constructed and reported separately, never
    registered in the unary runtime.  An available implementation absent from
    its static code-owned map aborts construction before a registration is
    returned.
    """

    # Revalidate paths, config, and every effective hash even if a caller used
    # Pydantic ``model_copy`` without validation.
    validated = LoadedTransformationRegistry.model_validate(loaded.model_dump(mode="json"))
    repo_root = find_repo_root(validated.registry_path.parent)
    configured_rules = _configured_negative_rules(validated)

    pair_configs = tuple(rule for rule in configured_rules if _is_pair_aware(rule))
    pair_aware = tuple(sorted(rule.rule_id for rule in pair_configs))
    unary = tuple(rule for rule in configured_rules if not _is_pair_aware(rule))
    available = tuple(
        rule for rule in unary if rule.implementation_status == RuleImplementationStatus.AVAILABLE
    )
    skipped = tuple(
        sorted(
            rule.rule_id
            for rule in unary
            if rule.implementation_status != RuleImplementationStatus.AVAILABLE
        )
    )

    unknown_keys = tuple(
        sorted(
            {
                rule.implementation_key
                for rule in available
                if rule.implementation_key not in _CODE_OWNED_UNARY_NEGATIVE_BUILDERS
            }
        )
    )
    if unknown_keys:
        raise NegativeRuleFactoryError(
            "available unary negative rules name non-code-owned implementations: "
            + ",".join(unknown_keys)
        )
    implementation_keys = tuple(rule.implementation_key for rule in available)
    if len(set(implementation_keys)) != len(implementation_keys):
        raise NegativeRuleFactoryError(
            "an available unary negative implementation_key is configured more than once"
        )

    unary_implementations: list[tuple[TransformationRuleConfig, TransformationRule]] = []
    for configured in sorted(available, key=lambda item: item.rule_id):
        builder = _CODE_OWNED_UNARY_NEGATIVE_BUILDERS[configured.implementation_key]
        try:
            implementation = builder(validated, repo_root)
        except Exception as exc:
            raise NegativeRuleFactoryError(
                "failed to construct code-owned unary negative implementation "
                f"{configured.implementation_key!r}"
            ) from exc
        _validate_implementation_identity(
            implementation,
            configured,
            validated.registry_hash,
        )
        unary_implementations.append((configured, implementation))

    pair_implementations: list[PairTransformationRule] = []
    for configured in sorted(pair_configs, key=lambda item: item.rule_id):
        if configured.implementation_status != RuleImplementationStatus.AVAILABLE:
            continue
        pair_builder = _CODE_OWNED_PAIR_NEGATIVE_BUILDERS.get(configured.implementation_key)
        if pair_builder is None:
            raise NegativeRuleFactoryError(
                "available pair-aware negative rule names non-code-owned implementation: "
                f"{configured.implementation_key}"
            )
        try:
            pair_implementation = pair_builder(validated, repo_root)
        except Exception as exc:
            raise NegativeRuleFactoryError(
                "failed to construct code-owned pair-aware negative implementation "
                f"{configured.implementation_key!r}"
            ) from exc
        if not isinstance(pair_implementation, PairTransformationRule):
            raise NegativeRuleFactoryError(
                f"implementation {configured.implementation_key!r} does not satisfy "
                "PairTransformationRule"
            )
        if isinstance(pair_implementation, TransformationRule):
            raise NegativeRuleFactoryError(
                f"pair-aware implementation {configured.implementation_key!r} "
                "must not satisfy the unary TransformationRule"
            )
        _validate_implementation_identity(
            pair_implementation,
            configured,
            validated.registry_hash,
        )
        pair_implementations.append(pair_implementation)

    runtime = TransformationRegistry(validated, rejection_sink=rejection_sink)
    registered: list[str] = []
    for configured, implementation in unary_implementations:
        runtime.register(implementation)
        registered.append(configured.rule_id)
    return NegativeRuleRegistration(
        runtime=runtime,
        registered_rule_ids=tuple(registered),
        skipped_rule_ids=skipped,
        pair_aware_rule_ids=pair_aware,
        pair_rules=tuple(pair_implementations),
        registry_hash=validated.registry_hash,
    )


__all__ = [
    "NegativeRuleFactoryError",
    "NegativeRuleRegistration",
    "build_negative_rule_runtime",
]
