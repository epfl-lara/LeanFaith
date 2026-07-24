"""Parse and enforce certificate dependency/axiom policy (PLAN.md §16.7)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

_AXIOMS = re.compile(r"depends on axioms:\s*\[(?P<items>[^\]]*)\]")
_NO_AXIOMS = re.compile(r"does not depend on any axioms")


@dataclass(frozen=True, slots=True)
class CertificateAudit:
    """Deterministic audit of a freshly printed Lean certificate."""

    certificate_name: str
    direct_constants: tuple[str, ...]
    transitive_constants: tuple[str, ...]
    axioms: tuple[str, ...]
    checks: dict[str, bool]
    violation_codes: tuple[str, ...]
    forbidden_constant_hits: tuple[str, ...]

    @property
    def accepted(self) -> bool:
        return all(self.checks.values()) and not self.violation_codes


def _message_texts(
    messages: tuple[dict[str, Any], ...] | list[dict[str, Any]],
) -> tuple[str, ...]:
    return tuple(
        str(message.get("data", ""))
        for message in messages
        if str(message.get("severity", "")) == "info"
    )


def _parse_axioms(certificate_name: str, texts: tuple[str, ...]) -> tuple[tuple[str, ...], bool]:
    marker = f"'{certificate_name}'"
    reports: list[tuple[str, ...]] = []
    for text in texts:
        if marker not in text:
            continue
        if _NO_AXIOMS.search(text):
            reports.append(())
            continue
        match = _AXIOMS.search(text)
        if match is not None:
            values = tuple(
                sorted(
                    item.strip().strip("`")
                    for item in match.group("items").split(",")
                    if item.strip()
                )
            )
            reports.append(values)
    if len(reports) != 1:
        return (), False
    return reports[0], True


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate audit JSON key {key!r}")
        value[key] = item
    return value


def _dependency_report(
    certificate_name: str,
    texts: tuple[str, ...],
) -> tuple[tuple[str, ...], tuple[str, ...], bool]:
    reports: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    for text in texts:
        if not text.startswith("LFAUDIT "):
            continue
        try:
            value = json.loads(
                text.removeprefix("LFAUDIT "),
                object_pairs_hook=_reject_duplicate_json_keys,
            )
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(value, dict):
            continue
        if value.get("name") != certificate_name:
            continue
        direct = value.get("direct_constants")
        transitive = value.get("transitive_constants")
        if not isinstance(direct, list) or not isinstance(transitive, list):
            continue
        if not all(isinstance(item, str) for item in (*direct, *transitive)):
            continue
        reports.append(
            (
                tuple(sorted(set(direct))),
                tuple(sorted(set(transitive))),
            )
        )
    if len(reports) != 1:
        return (), (), False
    direct, transitive = reports[0]
    return direct, transitive, True


def audit_certificate_messages(
    *,
    certificate_name: str,
    messages: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    allowed_axioms: tuple[str, ...],
    forbidden_axioms: tuple[str, ...],
    forbidden_constants: tuple[str, ...],
    has_sorries: bool = False,
) -> CertificateAudit:
    """Audit the exact ``#print`` and ``#print axioms`` response.

    Any missing print output fails closed.  Source/candidate theorem
    constants are rejected even if an automated tactic happened to discover
    them through an imported module.
    """

    texts = _message_texts(messages)
    axioms, axiom_report_present = _parse_axioms(certificate_name, texts)
    direct_constants, transitive_constants, dependency_report_present = _dependency_report(
        certificate_name, texts
    )
    allowed = set(allowed_axioms)
    forbidden = set(forbidden_axioms)
    disallowed_axioms = tuple(
        sorted(axiom for axiom in axioms if axiom in forbidden or axiom not in allowed)
    )
    constant_hits = tuple(
        sorted(
            constant
            for constant in set(forbidden_constants)
            if constant in set(transitive_constants)
        )
    )
    admission_constants = {
        constant
        for constant in transitive_constants
        if constant == "sorryAx" or constant.endswith(".sorryAx")
    }
    has_admission = bool(admission_constants) or has_sorries
    checks = {
        "dependency_report_present": dependency_report_present,
        "axiom_report_present": axiom_report_present,
        "no_forbidden_axioms": not disallowed_axioms,
        "no_source_or_candidate_constant": not constant_hits,
        "no_admission": not has_admission,
        # Lean accepted the freshly declared theorem with allow_sorry=False.
        # Unresolved metavariables would make the request invalid.
        "no_unresolved_metavariable": dependency_report_present,
    }
    violations: list[str] = []
    if not dependency_report_present:
        violations.append("dependency_report_missing")
    if not axiom_report_present:
        violations.append("axiom_report_missing")
    if disallowed_axioms:
        violations.extend(f"forbidden_axiom:{axiom}" for axiom in disallowed_axioms)
    if constant_hits:
        violations.extend(f"forbidden_constant:{name}" for name in constant_hits)
    if has_admission:
        violations.append("admission_detected")
    return CertificateAudit(
        certificate_name=certificate_name,
        direct_constants=direct_constants,
        transitive_constants=transitive_constants,
        axioms=axioms,
        checks=checks,
        violation_codes=tuple(violations),
        forbidden_constant_hits=constant_hits,
    )
