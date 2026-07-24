"""LF-012: pure declaration-extraction logic (proof stripping, IDs, failures)."""

from __future__ import annotations

import datetime

import pytest

from leanfaith.lean.extraction import (
    ExtractedDeclaration,
    ExtractionFailure,
    ExtractionFailureCode,
    SourceIdentity,
    build_theorem_record,
    extract_from_declarations,
    pos_to_offset,
    strip_proof,
)
from leanfaith.lean.extraction import _line_starts as line_starts
from leanfaith.schemas import ValidationStatus, is_valid_id

_UTC = datetime.datetime(2026, 7, 11, tzinfo=datetime.UTC)
_CTX = "ctx:" + "0" * 64


def _identity(**overrides: object) -> SourceIdentity:
    payload: dict[str, object] = {
        "source": "mathlib",
        "source_revision": "d568c8c0",
        "source_record": "Mathlib/Logic/Basic.lean",
        "context_id": _CTX,
        "source_file": "Mathlib/Logic/Basic.lean",
    }
    payload.update(overrides)
    return SourceIdentity(**payload)  # type: ignore[arg-type]


def _decl(
    source: str,
    *,
    name: str = "t",
    kind: str = "theorem",
    decl: tuple[int, int, int, int],
    sig: tuple[int, int, int, int],
    sig_pp: str = "(x : Nat) : x = x",
    type_pp: str = "x = x",
) -> dict[str, object]:
    def rng(t: tuple[int, int, int, int]) -> dict[str, object]:
        return {
            "start": {"line": t[0], "column": t[1]},
            "finish": {"line": t[2], "column": t[3]},
        }

    return {
        "name": name,
        "full_name": name,
        "kind": kind,
        "range": rng(decl),
        "signature": {"pp": sig_pp, "range": rng(sig)},
        "type": {"pp": type_pp},
    }


# --- offset math ---


def test_line_starts_and_pos() -> None:
    source = "abc\ndef\nghi"
    starts = line_starts(source)
    assert starts == [0, 4, 8]
    assert pos_to_offset(source, starts, 1, 0) == 0
    assert pos_to_offset(source, starts, 2, 1) == 5
    assert pos_to_offset(source, starts, 3, 2) == 10


def test_pos_out_of_range() -> None:
    source = "one line"
    with pytest.raises(ValueError, match="out of range"):
        pos_to_offset(source, line_starts(source), 5, 0)


# --- strip_proof (canned from real probe output) ---


def test_strip_by_proof() -> None:
    src = "theorem t_by (x y : Nat) : x + y = y + x := by omega"
    decl = _decl(src, name="t_by", decl=(1, 0, 1, 52), sig=(1, 13, 1, 40))
    assert strip_proof(src, decl) == "theorem t_by (x y : Nat) : x + y = y + x := by sorry"


def test_strip_term_proof() -> None:
    src = "theorem t_term (n : Nat) : n = n := rfl"
    decl = _decl(src, name="t_term", decl=(1, 0, 1, 39), sig=(1, 15, 1, 32))
    assert strip_proof(src, decl) == "theorem t_term (n : Nat) : n = n := by sorry"


def test_strip_multiline_preserves_signature_layout() -> None:
    src = "theorem t_ml (x y : Nat) :\n    x + y = y + x := by\n  omega"
    decl = _decl(src, name="t_ml", decl=(1, 0, 3, 7), sig=(1, 13, 2, 17))
    stripped = strip_proof(src, decl)
    assert stripped == "theorem t_ml (x y : Nat) :\n    x + y = y + x := by sorry"


def test_strip_attribute_kept() -> None:
    src = "@[simp] theorem t_attr (n : Nat) : n + 0 = n := by omega"
    decl = _decl(src, name="t_attr", decl=(1, 0, 1, 56), sig=(1, 23, 1, 44))
    assert strip_proof(src, decl).startswith("@[simp] theorem t_attr")
    assert strip_proof(src, decl).endswith(":= by sorry")


def test_strip_docstring_kept() -> None:
    src = "/-- doc comment -/\ntheorem t_doc : True := trivial"
    decl = _decl(src, name="t_doc", decl=(1, 0, 2, 31), sig=(2, 14, 2, 20))
    stripped = strip_proof(src, decl)
    assert stripped.startswith("/-- doc comment -/")
    assert stripped.endswith(": True := by sorry")


def test_strip_nonbmp_unicode() -> None:
    src = "theorem nonbmp (𝓝x : Nat) : 𝓝x = 𝓝x := rfl"
    decl = _decl(src, name="nonbmp", decl=(1, 0, 1, 41), sig=(1, 15, 1, 35))
    stripped = strip_proof(src, decl)
    assert stripped == "theorem nonbmp (𝓝x : Nat) : 𝓝x = 𝓝x := by sorry"


def test_strip_missing_signature_range_raises() -> None:
    decl = {"name": "t", "kind": "theorem", "range": {"start": {}, "finish": {}}}
    with pytest.raises(ValueError, match="signature"):
        strip_proof("theorem t : True := trivial", decl)


# --- build_theorem_record ---


def test_build_accepts_theorem() -> None:
    src = "theorem t_by (x y : Nat) : x + y = y + x := by omega"
    decl = _decl(src, name="t_by", decl=(1, 0, 1, 52), sig=(1, 13, 1, 40))
    built = build_theorem_record(
        _identity(),
        src,
        decl,
        elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
        created_at=_UTC,
    )
    assert isinstance(built, ExtractedDeclaration)
    assert built.theorem.is_proposition
    assert built.theorem.proof_stripped_declaration.endswith(":= by sorry")
    assert is_valid_id(built.theorem.theorem_id, prefix="thm")
    assert is_valid_id(built.theorem.ancestry_id, prefix="anc")
    assert built.theorem.root_ancestry_ids == (built.theorem.ancestry_id,)
    assert built.representation.raw_proof_stripped == built.proof_stripped


def test_build_rejects_definition() -> None:
    src = "def d_foo (n : Nat) : Nat := n + 1"
    decl = _decl(src, name="d_foo", kind="definition", decl=(1, 0, 1, 34), sig=(1, 10, 1, 25))
    built = build_theorem_record(
        _identity(), src, decl, elaboration_status=ValidationStatus.ELABORATES, created_at=_UTC
    )
    assert isinstance(built, ExtractionFailure)
    assert built.code is ExtractionFailureCode.NOT_A_PROPOSITION


def test_ids_are_deterministic_and_provenance_sensitive() -> None:
    src = "theorem t_by (x y : Nat) : x + y = y + x := by omega"
    decl = _decl(src, name="t_by", decl=(1, 0, 1, 52), sig=(1, 13, 1, 40))
    a = build_theorem_record(
        _identity(),
        src,
        decl,
        elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
        created_at=_UTC,
    )
    b = build_theorem_record(
        _identity(),
        src,
        decl,
        elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
        created_at=_UTC,
    )
    c = build_theorem_record(
        _identity(source_revision="other"),
        src,
        decl,
        elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
        created_at=_UTC,
    )
    assert isinstance(a, ExtractedDeclaration)
    assert isinstance(b, ExtractedDeclaration)
    assert isinstance(c, ExtractedDeclaration)
    assert a.theorem.theorem_id == b.theorem.theorem_id
    assert a.theorem.theorem_id != c.theorem.theorem_id  # revision enters the ID

    ordinal_one = build_theorem_record(
        _identity(),
        src,
        decl,
        elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
        created_at=_UTC,
        declaration_ordinal=1,
    )
    assert isinstance(ordinal_one, ExtractedDeclaration)
    assert a.theorem.declaration_ordinal == 0
    assert ordinal_one.theorem.declaration_ordinal == 1
    assert a.theorem.theorem_id != ordinal_one.theorem.theorem_id


# --- extract_from_declarations ---


def test_extract_selects_props_and_quarantines_duplicates() -> None:
    src = (
        "theorem good (x : Nat) : x = x := rfl\n"
        "def helper (n : Nat) : Nat := n\n"
        "theorem dup : True := trivial\n"
        "theorem dup : True := trivial"
    )
    decls = [
        _decl(src, name="good", decl=(1, 0, 1, 37), sig=(1, 13, 1, 30)),
        _decl(src, name="helper", kind="definition", decl=(2, 0, 2, 30), sig=(2, 11, 2, 26)),
        _decl(src, name="dup", decl=(3, 0, 3, 29), sig=(3, 12, 3, 18)),
        _decl(src, name="dup", decl=(4, 0, 4, 29), sig=(4, 12, 4, 18)),
    ]
    result = extract_from_declarations(_identity(), src, decls, created_at=_UTC)
    accepted_names = {d.theorem.declaration_name for d in result.accepted}
    assert accepted_names == {"good"}  # def rejected explicitly, dup quarantined
    failure_codes = {f.code for f in result.failures}
    assert ExtractionFailureCode.NOT_A_PROPOSITION in failure_codes
    assert ExtractionFailureCode.DUPLICATE_DECLARATION_NAME in failure_codes


def test_quality_flags_trivial_conclusion() -> None:
    src = "theorem t_triv : True := trivial"
    decl = _decl(src, name="t_triv", decl=(1, 0, 1, 32), sig=(1, 14, 1, 20), type_pp="True")
    built = build_theorem_record(
        _identity(),
        src,
        decl,
        elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
        created_at=_UTC,
    )
    assert isinstance(built, ExtractedDeclaration)
    assert built.theorem.metadata["trivial_conclusion"] is True
    assert built.theorem.metadata["transform_source_eligible"] is False


def test_quality_flags_autoparam_tactic() -> None:
    # A binder default hiding a tactic block: the : True conclusion is worthless.
    src = "theorem bad (n : Nat := by exact 0) : True := by sorry"
    decl = _decl(src, name="bad", decl=(1, 0, 1, 53), sig=(1, 12, 1, 43), type_pp="True")
    built = build_theorem_record(
        _identity(),
        src,
        decl,
        elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
        created_at=_UTC,
    )
    assert isinstance(built, ExtractedDeclaration)
    assert built.theorem.metadata["autoparam_tactic_in_signature"] is True
    assert built.theorem.metadata["transform_source_eligible"] is False


def test_quality_flags_clean_theorem_eligible() -> None:
    src = "theorem good (x y : Nat) : x + y = y + x := by omega"
    decl = _decl(src, name="good", decl=(1, 0, 1, 52), sig=(1, 13, 1, 40), type_pp="x + y = y + x")
    built = build_theorem_record(
        _identity(),
        src,
        decl,
        elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
        created_at=_UTC,
    )
    assert isinstance(built, ExtractedDeclaration)
    assert built.theorem.metadata["transform_source_eligible"] is True
