"""Deterministic Lean source builders for symbolic evidence (PLAN.md §16).

The compared theorem constants and proof bodies never appear in these
programs.  Each job recreates only the two proposition *types* as fresh,
reducible aliases and proves a fresh local certificate.  The certificate is
then printed so the caller can reject accidental dependencies on either
source theorem constant.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

from leanfaith.config.hashing import sha256_hex
from leanfaith.config.paths import find_repo_root

Direction = Literal["A_to_B", "B_to_A"]
SeparatorDirection = Literal["A_to_B", "B_to_A", "equivalence_only"]

_LEAN_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_']*$")
_GENERATED_UNIVERSE = re.compile(r"\bu_[0-9]+\b")


class EvidenceCommandError(ValueError):
    """A symbolic-evidence command cannot be constructed safely."""


@dataclass(frozen=True, slots=True)
class PropositionPairSource:
    """The proof-free inputs used to construct one evidence request."""

    header_text: str
    proposition_a: str
    proposition_b: str
    pair_id: str
    forbidden_declaration_constants: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.proposition_a.strip() or not self.proposition_b.strip():
            raise EvidenceCommandError("both proposition expressions must be nonempty")
        if "\x00" in self.header_text + self.proposition_a + self.proposition_b:
            raise EvidenceCommandError("Lean source cannot contain NUL bytes")


@dataclass(frozen=True, slots=True)
class RenderedEvidenceCommand:
    """Generated Lean source plus the local names required for auditing."""

    code: str
    code_sha256: str
    alias_a: str
    alias_b: str
    certificate_name: str | None = None


def _local_suffix(pair_id: str) -> str:
    digest = sha256_hex(pair_id.encode("utf-8"))[:16]
    return digest


def _universe_declaration(*expressions: str) -> str:
    """Recover Gate-3 generated universe placeholders.

    ``signature_explicit`` is produced by Lean and normalizes anonymous
    universes to ``u_<n>``.  Re-declaring exactly those names makes the
    pretty-printed proposition reusable without carrying a theorem proof.
    User-written named universes are already represented by binders in the
    declaration context; the MVP intentionally does not guess arbitrary
    identifiers.
    """

    names = sorted(
        {match.group(0) for text in expressions for match in _GENERATED_UNIVERSE.finditer(text)}
    )
    return "" if not names else "universe " + " ".join(names)


def _aliases(source: PropositionPairSource) -> tuple[str, str, str]:
    suffix = _local_suffix(source.pair_id)
    alias_a = f"LeanFaithEvidenceA_{suffix}"
    alias_b = f"LeanFaithEvidenceB_{suffix}"
    universes = _universe_declaration(source.proposition_a, source.proposition_b)
    parts = [
        "import Lean",
        source.header_text.strip(),
        universes,
        f"abbrev {alias_a} : Prop := ({source.proposition_a.strip()})",
        f"abbrev {alias_b} : Prop := ({source.proposition_b.strip()})",
    ]
    return alias_a, alias_b, "\n\n".join(part for part in parts if part)


@lru_cache(maxsize=1)
def proof_audit_helper_source() -> str:
    """Return the import-stripped canonical Lean dependency-audit helper."""

    path = find_repo_root(Path(__file__).parent) / "LeanFaith" / "Meta" / "ProofChecks.lean"
    lines = path.read_text(encoding="utf-8").splitlines()
    return "\n".join(line for line in lines if not line.startswith("import "))


def _render(
    *,
    code: str,
    alias_a: str,
    alias_b: str,
    certificate_name: str | None = None,
) -> RenderedEvidenceCommand:
    normalized = code.rstrip() + "\n"
    return RenderedEvidenceCommand(
        code=normalized,
        code_sha256=sha256_hex(normalized.encode("utf-8")),
        alias_a=alias_a,
        alias_b=alias_b,
        certificate_name=certificate_name,
    )


def render_alias_preflight(source: PropositionPairSource) -> RenderedEvidenceCommand:
    """Elaborate both proposition aliases without making any semantic claim."""

    alias_a, alias_b, prefix = _aliases(source)
    code = f"{prefix}\n\n#check {alias_a}\n#check {alias_b}"
    return _render(code=code, alias_a=alias_a, alias_b=alias_b)


def render_defeq_check(source: PropositionPairSource) -> RenderedEvidenceCommand:
    """Check definitional equality by kernel ``rfl`` after alias preflight."""

    alias_a, alias_b, prefix = _aliases(source)
    suffix = _local_suffix(source.pair_id)
    certificate = f"LeanFaithDefEq_{suffix}"
    code = (
        f"{prefix}\n\n"
        f"theorem {certificate} : {alias_a} = {alias_b} := by\n"
        "  rfl\n"
        f"#print axioms {certificate}"
    )
    return _render(
        code=code,
        alias_a=alias_a,
        alias_b=alias_b,
        certificate_name=certificate,
    )


def render_directional_proof(
    source: PropositionPairSource,
    *,
    direction: Direction,
    tactic_body: str,
    method_id: str,
) -> RenderedEvidenceCommand:
    """Render one replayable, admission-free whole-proposition implication."""

    if not _LEAN_IDENTIFIER.fullmatch(method_id):
        raise EvidenceCommandError(f"invalid proof method identifier: {method_id!r}")
    if not tactic_body.strip():
        raise EvidenceCommandError("proof tactic body must be nonempty")
    alias_a, alias_b, prefix = _aliases(source)
    suffix = _local_suffix(source.pair_id)
    certificate = f"LeanFaithProof_{method_id}_{direction}_{suffix}"
    left, right = (alias_a, alias_b) if direction == "A_to_B" else (alias_b, alias_a)
    indented = "\n".join(f"  {line}" for line in tactic_body.strip().splitlines())
    code = (
        f"{prefix}\n\n"
        f"{proof_audit_helper_source()}\n\n"
        f"theorem {certificate} : {left} → {right} := by\n"
        f"{indented}\n"
        f'lfProofAudit "{certificate}"\n'
        f"#print axioms {certificate}"
    )
    return _render(
        code=code,
        alias_a=alias_a,
        alias_b=alias_b,
        certificate_name=certificate,
    )


def _separator_expression(
    alias_a: str,
    alias_b: str,
    direction: SeparatorDirection,
) -> str:
    if direction == "A_to_B":
        # A true while B false refutes A → B.
        return f"({alias_a} ∧ ¬ {alias_b})"
    if direction == "B_to_A":
        # B true while A false refutes B → A.
        return f"({alias_b} ∧ ¬ {alias_a})"
    return f"(Or ({alias_a} ∧ ¬ {alias_b}) ({alias_b} ∧ ¬ {alias_a}))"


def render_counterexample_preflight(
    source: PropositionPairSource,
    *,
    direction: SeparatorDirection,
) -> RenderedEvidenceCommand:
    """Check that the registered separator proposition is kernel-decidable."""

    alias_a, alias_b, prefix = _aliases(source)
    separator = _separator_expression(alias_a, alias_b, direction)
    code = f"{prefix}\n\n#synth Decidable {separator}"
    return _render(code=code, alias_a=alias_a, alias_b=alias_b)


def render_counterexample_check(
    source: PropositionPairSource,
    *,
    direction: SeparatorDirection,
) -> RenderedEvidenceCommand:
    """Attempt a kernel ``decide`` separator; never uses ``native_decide``."""

    alias_a, alias_b, prefix = _aliases(source)
    suffix = _local_suffix(source.pair_id)
    certificate = f"LeanFaithSeparator_{direction}_{suffix}"
    separator = _separator_expression(alias_a, alias_b, direction)
    code = (
        f"{prefix}\n\n"
        f"{proof_audit_helper_source()}\n\n"
        f"theorem {certificate} : {separator} := by\n"
        "  decide\n"
        f'lfProofAudit "{certificate}"\n'
        f"#print axioms {certificate}"
    )
    return _render(
        code=code,
        alias_a=alias_a,
        alias_b=alias_b,
        certificate_name=certificate,
    )
