from leanfaith.sft2b import pilot_source_freeze as freeze


def test_closed_proposition_closes_binders() -> None:
    assert freeze._closed_proposition("(x : ℝ) (h : 0 < x) : x ≠ 0") == (
        "∀ (x : ℝ) (h : 0 < x), x ≠ 0"
    )
    assert freeze._closed_proposition(": True") == "True"


def test_parse_header_keeps_only_structured_context() -> None:
    observed = freeze._parse_header(
        "import Mathlib\nimport Aesop\nopen Real Set\n"
        "open scoped BigOperators\nset_option maxHeartbeats 0\n"
    )
    assert observed == (
        "import Mathlib\nimport Aesop\n",
        ("Real", "Set"),
        ("BigOperators",),
        {"maxHeartbeats": 0},
    )
    assert freeze._parse_header("import Mathlib\ndef helper := 1\n") is None


def test_nested_adjacent_comment_recovery() -> None:
    source = "import Mathlib\n/- outer /- nested -/ claim -/\ntheorem t : True := by trivial"
    comments = freeze._block_comments(source)
    assert comments == [(15, 45, " outer /- nested -/ claim ")]


def test_standalone_quality_rejects_cross_references_and_solutions() -> None:
    assert (
        freeze._standalone_nl(
            "Every finite subgroup of the multiplicative group is cyclic.",
            mathlib_docstring=True,
        )
        is not None
    )
    assert (
        freeze._standalone_nl(
            "For a diagram explaining the variables, see the module docstring.",
            mathlib_docstring=True,
        )
        is None
    )
    assert (
        freeze._standalone_nl(
            "Prove the inequality. First we use induction and therefore we conclude the claim.",
            mathlib_docstring=False,
        )
        is None
    )
