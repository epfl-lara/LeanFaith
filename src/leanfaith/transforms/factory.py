"""Static, fail-closed construction of code-owned LF-017 positive rules.

YAML selects among implementations compiled into this module; it can never
name an import path or cause dynamic loading.  Only rules explicitly marked
``available`` are constructed and registered.  Every constructed rule is
bound to the effective registry hash and checked against its configured
identity before it reaches the guarded runtime registry.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from leanfaith.config.paths import find_repo_root
from leanfaith.schemas.enums import Polarity
from leanfaith.transforms.p01_alpha import P01AlphaRule
from leanfaith.transforms.positives.p02_binders import P02BinderRule
from leanfaith.transforms.positives.p04_notation_lite import P04NotationLiteRule
from leanfaith.transforms.protocol import TransformationRule
from leanfaith.transforms.registry import (
    LoadedTransformationRegistry,
    RejectionSink,
    RuleImplementationStatus,
    TransformationRegistry,
    TransformationRuleConfig,
)


class PositiveRuleFactoryError(ValueError):
    """Configured positive implementation cannot be safely constructed."""


RuleBuilder = Callable[
    [LoadedTransformationRegistry, Path],
    TransformationRule,
]


def _build_p01(
    loaded: LoadedTransformationRegistry,
    repo_root: Path,
) -> TransformationRule:
    return P01AlphaRule.from_repository(
        generation_config_hash=loaded.registry_hash,
        repo_root=repo_root,
    )


def _build_p02(
    loaded: LoadedTransformationRegistry,
    repo_root: Path,
) -> TransformationRule:
    del repo_root
    return P02BinderRule(registry_hash=loaded.registry_hash)


def _build_p04(
    loaded: LoadedTransformationRegistry,
    repo_root: Path,
) -> TransformationRule:
    return P04NotationLiteRule.from_repository(
        generation_config_hash=loaded.registry_hash,
        repo_root=repo_root,
    )


# This is the only implementation-name boundary.  Never populate it from YAML,
# entry points, module paths, or user-provided strings.
_CODE_OWNED_POSITIVE_BUILDERS: Mapping[str, RuleBuilder] = MappingProxyType(
    {
        "p01_alpha": _build_p01,
        "p02_binders": _build_p02,
        "p04_notation_lite": _build_p04,
    }
)


@dataclass(frozen=True, slots=True)
class PositiveRuleRegistration:
    """A guarded runtime plus its deterministic registration inventory."""

    runtime: TransformationRegistry
    registered_rule_ids: tuple[str, ...]
    skipped_rule_ids: tuple[str, ...]
    registry_hash: str


def _configured_positive_rules(
    loaded: LoadedTransformationRegistry,
) -> tuple[TransformationRuleConfig, ...]:
    return tuple(
        rule
        for family in loaded.config.families
        if family.polarity == Polarity.POSITIVE
        for rule in family.rules
    )


def _validate_implementation_identity(
    implementation: TransformationRule,
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
        raise PositiveRuleFactoryError(
            f"implementation {configured.implementation_key!r} metadata mismatch: "
            f"{','.join(mismatches)}"
        )
    bound_hashes = tuple(
        value
        for field_name in ("generation_config_hash", "registry_hash")
        if isinstance((value := getattr(implementation, field_name, None)), str)
    )
    if not bound_hashes:
        raise PositiveRuleFactoryError(
            f"implementation {configured.implementation_key!r} exposes no registry-hash binding"
        )
    if any(value != effective_registry_hash for value in bound_hashes):
        raise PositiveRuleFactoryError(
            f"implementation {configured.implementation_key!r} is not bound to "
            "the effective registry hash"
        )


def build_positive_rule_runtime(
    loaded: LoadedTransformationRegistry,
    *,
    rejection_sink: RejectionSink | None = None,
) -> PositiveRuleRegistration:
    """Construct and register every configured available positive rule.

    Pending and disabled positive rules are recorded as skipped and are never
    imported or instantiated dynamically.  An available positive
    ``implementation_key`` absent from the static code-owned map aborts the
    entire construction before a runtime is returned.
    """

    # Revalidate paths, config, and every effective hash even if a caller used
    # Pydantic ``model_copy`` without validation.
    validated = LoadedTransformationRegistry.model_validate(loaded.model_dump(mode="json"))
    repo_root = find_repo_root(validated.registry_path.parent)
    configured_rules = _configured_positive_rules(validated)
    available = tuple(
        rule
        for rule in configured_rules
        if rule.implementation_status == RuleImplementationStatus.AVAILABLE
    )
    skipped = tuple(
        sorted(
            rule.rule_id
            for rule in configured_rules
            if rule.implementation_status != RuleImplementationStatus.AVAILABLE
        )
    )

    unknown_keys = tuple(
        sorted(
            {
                rule.implementation_key
                for rule in available
                if rule.implementation_key not in _CODE_OWNED_POSITIVE_BUILDERS
            }
        )
    )
    if unknown_keys:
        raise PositiveRuleFactoryError(
            "available positive rules name non-code-owned implementations: "
            + ",".join(unknown_keys)
        )
    implementation_keys = tuple(rule.implementation_key for rule in available)
    if len(set(implementation_keys)) != len(implementation_keys):
        raise PositiveRuleFactoryError(
            "an available positive implementation_key is configured more than once"
        )

    implementations: list[tuple[TransformationRuleConfig, TransformationRule]] = []
    for configured in sorted(available, key=lambda item: item.rule_id):
        builder = _CODE_OWNED_POSITIVE_BUILDERS[configured.implementation_key]
        try:
            implementation = builder(validated, repo_root)
        except Exception as exc:
            raise PositiveRuleFactoryError(
                f"failed to construct code-owned implementation {configured.implementation_key!r}"
            ) from exc
        _validate_implementation_identity(
            implementation,
            configured,
            validated.registry_hash,
        )
        implementations.append((configured, implementation))

    runtime = TransformationRegistry(validated, rejection_sink=rejection_sink)
    registered: list[str] = []
    for configured, implementation in implementations:
        runtime.register(implementation)
        registered.append(configured.rule_id)
    return PositiveRuleRegistration(
        runtime=runtime,
        registered_rule_ids=tuple(registered),
        skipped_rule_ids=skipped,
        registry_hash=validated.registry_hash,
    )


__all__ = [
    "PositiveRuleFactoryError",
    "PositiveRuleRegistration",
    "build_positive_rule_runtime",
]
