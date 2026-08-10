"""Conservative experimental E0 surface rules P06 and P10.

The executable scope is deliberately smaller than the design envelope in
``configs/transformations/v2.yaml``:

* P06 omits already surfaced *ordinary* implicit type arguments from a small,
  code-owned allowlist of functions whose declarations have no instance,
  ``autoParam``, or ``optParam`` arguments.  It never guesses an omitted type.
* P10 switches ``Prod`` anonymous-constructor and tuple syntax only when an
  explicit product ascription makes the expected single-constructor type
  unambiguous.

Both rules replace one contiguous source span and store its exact inverse.
They reuse the E0 mechanical audit, so a candidate must independently
re-elaborate in the same context with identical alpha-canonical theorem type
and semantic atoms.  A clean result remains provisional: this module emits no
resolved label, promotion, or training eligibility.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from leanfaith.transforms.positives.v2_e0 import (
    PresentationSite,
    _E0PresentationRule,
    _signature_bounds,
)

_IDENTIFIER = r"[A-Za-z][A-Za-z0-9_']*(?:\.[A-Za-z][A-Za-z0-9_']*)*"
_ATOM = rf"(?:{_IDENTIFIER}|[0-9]+)"
_ROOT_PREFIX = re.compile(r"(?:=|\u2260|<|>|\u2264|\u2265|\u2194|\u2192|\u2227|\u2228|,)\s*$")


@dataclass(frozen=True, slots=True)
class _ImplicitHead:
    name: str
    ordinary_implicit_count: int
    explicit_argument_count: int


# Each entry is intentionally declaration-specific.  These functions expose
# only ordinary type implicits before their explicit arguments in the Lean
# versions supported by this project.  Constructors stay owned by P10, and
# functions with instance/auto/optional parameters are absent by construction.
_P06_HEADS: tuple[_ImplicitHead, ...] = (
    _ImplicitHead("Function.id", 1, 1),
    _ImplicitHead("List.append", 1, 2),
    _ImplicitHead("List.length", 1, 1),
    _ImplicitHead("List.map", 2, 2),
    _ImplicitHead("List.reverse", 1, 1),
    _ImplicitHead("Prod.fst", 2, 1),
    _ImplicitHead("Prod.snd", 2, 1),
)


def _direct_application_context(prefix: str) -> bool:
    """Accept a root expression or one direct root-connective operand."""

    stripped = prefix.rstrip()
    return not stripped or _ROOT_PREFIX.search(stripped) is not None


def _ends_application(source: str, end: int) -> bool:
    """Reject a matched prefix that is followed by another application arg."""

    remainder = source[end:].lstrip()
    if not remainder:
        return True
    return remainder.startswith(
        (
            ")",
            "]",
            "}",
            "⟄",
            ",",
            ":=",
            "=",
            "\u2260",
            "<",
            ">",
            "\u2264",
            "\u2265",
            "\u2194",
            "\u2192",
            "\u2227",
            "\u2228",
        )
    )


def _p06_pattern(head: _ImplicitHead) -> re.Pattern[str]:
    implicit = "".join(
        rf"\s+(?P<i{index}>{_ATOM})" for index in range(head.ordinary_implicit_count)
    )
    explicit = "".join(
        rf"\s+(?P<e{index}>{_ATOM})" for index in range(head.explicit_argument_count)
    )
    return re.compile(
        rf"(?<![A-Za-z0-9_'.])(?P<prefix>@(?P<head>{re.escape(head.name)}){implicit})"
        rf"(?P<explicit>{explicit})(?![A-Za-z0-9_'.])"
    )


_P06_PATTERNS = tuple((head, _p06_pattern(head)) for head in _P06_HEADS)


def enumerate_p06_sites(source: str) -> tuple[PresentationSite, ...]:
    """Find exact ``@head implicit...`` prefixes safe to omit.

    Applicability is intentionally limited to atomic argument spines and root
    connective operands.  This rejects nested applications, coercion-shaped
    terms, metavariables, and all non-allowlisted heads before Lean execution.
    """

    mask, conclusion_start, conclusion_end = _signature_bounds(source)
    segment = mask[conclusion_start:conclusion_end]
    sites: list[PresentationSite] = []
    for head, pattern in _P06_PATTERNS:
        for match in pattern.finditer(segment):
            absolute_match_start = conclusion_start + match.start()
            absolute_match_end = conclusion_start + match.end()
            if not _direct_application_context(mask[conclusion_start:absolute_match_start]):
                continue
            if not _ends_application(mask, absolute_match_end):
                continue
            implicit_arguments = tuple(
                match.group(f"i{index}") for index in range(head.ordinary_implicit_count)
            )
            explicit_arguments = tuple(
                match.group(f"e{index}") for index in range(head.explicit_argument_count)
            )
            if any(
                argument.startswith("_") for argument in (*implicit_arguments, *explicit_arguments)
            ):
                continue
            start = conclusion_start + match.start("prefix")
            end = conclusion_start + match.end("prefix")
            source_text = source[start:end]
            if "--" in source_text or "/-" in source_text:
                continue
            sites.append(
                PresentationSite(
                    operation="omit_ordinary_implicit_arguments",
                    start=start,
                    end=end,
                    source_text=source_text,
                    replacement_text=head.name,
                    metadata=(
                        ("application_head", head.name),
                        ("explicit_argument_count", str(head.explicit_argument_count)),
                        ("ordinary_implicit_argument_count", str(head.ordinary_implicit_count)),
                    ),
                )
            )
    return tuple(sorted(sites, key=lambda item: (item.start, item.end, item.operation)))


_P10_ANONYMOUS = re.compile(
    rf"\(\s*(?P<expr>⟨\s*(?P<left>{_ATOM})\s*,\s*(?P<right>{_ATOM})\s*⟩)"
    rf"\s*:\s*(?P<left_type>{_IDENTIFIER})\s*\u00d7\s*"
    rf"(?P<right_type>{_IDENTIFIER})\s*\)"
)
_P10_TUPLE = re.compile(
    rf"\(\s*(?P<expr>\(\s*(?P<left>{_ATOM})\s*,\s*(?P<right>{_ATOM})\s*\))"
    rf"\s*:\s*(?P<left_type>{_IDENTIFIER})\s*\u00d7\s*"
    rf"(?P<right_type>{_IDENTIFIER})\s*\)"
)


def enumerate_p10_sites(source: str) -> tuple[PresentationSite, ...]:
    """Find non-nested two-field ``Prod`` presentation sites.

    The mandatory product ascription is the fail-closed evidence that tuple
    syntax and the anonymous constructor target the same known one-constructor
    type.  Lean re-elaboration and exact alpha identity remain authoritative.
    """

    mask, conclusion_start, conclusion_end = _signature_bounds(source)
    segment = mask[conclusion_start:conclusion_end]
    sites: list[PresentationSite] = []
    for pattern, operation in (
        (_P10_ANONYMOUS, "anonymous_constructor_to_tuple"),
        (_P10_TUPLE, "tuple_to_anonymous_constructor"),
    ):
        for match in pattern.finditer(segment):
            left = match.group("left")
            right = match.group("right")
            if left.startswith("_") or right.startswith("_"):
                continue
            start = conclusion_start + match.start("expr")
            end = conclusion_start + match.end("expr")
            source_text = source[start:end]
            if "--" in source_text or "/-" in source_text:
                continue
            replacement = (
                f"({left}, {right})" if pattern is _P10_ANONYMOUS else f"⟨{left}, {right}⟩"
            )
            sites.append(
                PresentationSite(
                    operation=operation,
                    start=start,
                    end=end,
                    source_text=source_text,
                    replacement_text=replacement,
                    metadata=(
                        ("constructor", "Prod.mk"),
                        ("left_type", match.group("left_type")),
                        ("right_type", match.group("right_type")),
                    ),
                )
            )
    return tuple(sorted(sites, key=lambda item: (item.start, item.end, item.operation)))


class P06ImplicitArgumentsRule(_E0PresentationRule):
    """P06 ordinary-implicit omission; outputs remain provisional."""

    rule_id = "p06_implicit_arguments"
    family_id = "p06_implicit_arguments"
    implementation_key = "p06_implicit_arguments"

    def _sites(self, source: str) -> tuple[PresentationSite, ...]:
        return enumerate_p06_sites(source)


class P10ConstructorsRule(_E0PresentationRule):
    """P10 explicitly ascribed ``Prod`` presentation; outputs remain provisional."""

    rule_id = "p10_constructors"
    family_id = "p10_constructors"
    implementation_key = "p10_constructors"

    def _sites(self, source: str) -> tuple[PresentationSite, ...]:
        return enumerate_p10_sites(source)


__all__ = [
    "P06ImplicitArgumentsRule",
    "P10ConstructorsRule",
    "enumerate_p06_sites",
    "enumerate_p10_sites",
]
