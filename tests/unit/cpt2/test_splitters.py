from __future__ import annotations

from leanfaith.cpt2.splitters import (
    DECLARATION_AWARE_METHOD,
    MASKED_REVERSE_METHOD,
    RAW_REVERSE_METHOD,
    mask_lean_source,
    source_features,
    split_source,
)


def test_three_splitters_expose_expected_nested_proof_difference() -> None:
    source = (
        "lemma helper : True := by trivial\n"
        "theorem target : True := by\n"
        "  have inner : True := by trivial\n"
        "  exact inner\n"
    )
    raw = split_source(source, RAW_REVERSE_METHOD)
    masked = split_source(source, MASKED_REVERSE_METHOD)
    aware = split_source(source, DECLARATION_AWARE_METHOD)
    assert raw is not None and masked is not None and aware is not None
    assert raw.by_offset == masked.by_offset
    assert raw.by_offset > aware.by_offset
    for split in (raw, masked, aware):
        assert split.reconstruct() == source
        assert tuple({"theorem": split.theorem, "body": split.body, "label": True}) == (
            "theorem",
            "body",
            "label",
        )


def test_masked_reverse_ignores_fake_delimiters_in_nested_comments_and_strings() -> None:
    source = (
        'theorem target : True := by\n  let text := ":= by"\n'
        "  /- outer := by /- nested := by -/ end -/\n"
        "  exact True.intro\n"
    )
    raw = split_source(source, RAW_REVERSE_METHOD)
    masked = split_source(source, MASKED_REVERSE_METHOD)
    aware = split_source(source, DECLARATION_AWARE_METHOD)
    assert raw is not None and masked is not None and aware is not None
    assert raw.by_offset != aware.by_offset
    assert masked.by_offset == aware.by_offset
    assert ":= by" not in mask_lean_source('"fake := by"')


def test_declaration_aware_tolerates_comments_whitespace_and_default_tactics() -> None:
    source = (
        "@[simp] theorem target (n : Nat := by exact 1) : n = n := /- proof -/\n  by\n    rfl\n"
    )
    aware = split_source(source, DECLARATION_AWARE_METHOD)
    assert aware is not None
    assert aware.theorem.endswith(":= /- proof -/\n  ")
    assert aware.body == "\n    rfl\n"
    assert aware.reconstruct() == source


def test_character_and_quoted_identifier_masking_preserves_offsets() -> None:
    source = "#check ':='\n#check «fake := by»\ntheorem target : True := by trivial\n"
    masked = mask_lean_source(source)
    assert len(masked) == len(source)
    assert masked.count("\n") == source.count("\n")
    aware = split_source(source, DECLARATION_AWARE_METHOD)
    assert aware is not None
    assert source[aware.by_offset : aware.by_offset + 2] == "by"


def test_features_find_multiple_declarations_and_nested_by() -> None:
    source = "lemma a : True := by trivial\ntheorem b : True := by exact (by trivial)\n"
    split = split_source(source, DECLARATION_AWARE_METHOD)
    features = source_features(source, split)
    assert features["multiple_declarations"] is True
    assert features["nested_by"] is True
