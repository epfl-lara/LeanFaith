from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from leanfaith.config.hashing import sha256_hex
from leanfaith.sft2b.full_source_freeze import (
    QualifiedSource,
    _adjacent_docstring,
    _has_denied_marker,
    _matched_50k,
    _render_source_prompt,
    _replay_headless_proposition,
    _semantic_sample,
    _strict_library_nl,
    _workbook_discourse_disposition,
    _workbook_discourse_flags,
)
from leanfaith.sft2b.pilot_source_freeze import AuditedSource
from leanfaith.sft2b.schemas import CompileContextRecord, SourceProvenance, SourceRecord, stable_id

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CORRUPTION_FIXTURE = _REPO_ROOT / "configs/sft2b/library_docstring_corruption_v1.json"
_V1_BUNDLE = Path(
    "/storage/milikic/leanfaith/value_first/sft2_autoformalizer_v1/"
    "source_inputs/reform_diverse_full_v1"
)


def test_proofnet_family_is_blanket_excluded() -> None:
    assert _has_denied_marker("PAug/ProofNetVerif")
    assert _has_denied_marker("derived_proofnet_sharp_mix")
    assert _has_denied_marker("ProofNet# benchmark")
    assert _has_denied_marker("ShadowBench")
    assert not _has_denied_marker("NuminaMath-LEAN", "olympiads")


def test_prompt_renderer_does_not_confuse_nl_substrings_with_reference_leaks() -> None:
    source = _qualified(0, "test", "test").audited.record
    short_reference = source.model_copy(
        update={
            "reference_proposition": "natural number",
            "reference_proposition_sha256": sha256_hex(b"natural number"),
        }
    )
    assert short_reference.nl_statement in _render_source_prompt(
        "Formalize this:\n{{NL}}", short_reference
    )


def test_colon_headless_replay_preserves_default_binder_tactics() -> None:
    proposition = "∀ (a : A) (ha : P a := by exact defaultProof), Q a"
    assert _replay_headless_proposition(f": {proposition}") == proposition


def test_adjacent_docstring_and_strict_quality() -> None:
    source = (
        "/-- Every finite execution extracted from an infinite execution is valid. -/\n"
        "@[simp]\n"
        "theorem extracted_execution : True := by trivial\n"
    )
    doc = _adjacent_docstring(source, 3)
    assert doc is not None
    assert _strict_library_nl(doc) == (
        "Every finite execution extracted from an infinite execution is valid."
    )
    assert _strict_library_nl("Characterisation theorem for the semantics.") is None
    separated = "/-- A sufficiently long and complete mathematical statement is true. -/\n"
    separated += "def helper := 1\ntheorem t : True := by trivial\n"
    assert _adjacent_docstring(separated, 3) is None


def test_adjacent_docstring_does_not_join_across_an_ordinary_block_comment() -> None:
    source = (
        "/-- Substitution of a fresh variable leaves the term unchanged. -/\n"
        "theorem subst_fresh : True := by trivial\n"
        "\n"
        "/- Opening and closing are inverses. -/\n"
        "lemma open_close : True := by trivial\n"
    )
    assert _adjacent_docstring(source, 5) is None


def test_adjacent_docstring_ignores_nested_attribute_doc_comments() -> None:
    source = (
        "/-- Assumes left covariance and concludes the strict product bound. -/\n"
        "@[to_additive /-- Assumes left covariance for the additive theorem. -/]\n"
        "theorem mul_lt_one : True := by trivial\n"
    )
    assert _adjacent_docstring(source, 3) == (
        " Assumes left covariance and concludes the strict product bound. "
    )


def test_adjacent_docstring_matches_nested_lean_block_comments() -> None:
    source = (
        "/-- A complete outer explanation /- with a nested note -/ remains attached. -/\n"
        "theorem nested : True := by trivial\n"
    )
    doc = _adjacent_docstring(source, 2)
    assert doc == " A complete outer explanation /- with a nested note -/ remains attached. "
    # Even a structurally valid nested delimiter is rejected by the independent
    # model-facing library-NL canary.
    assert _strict_library_nl(doc) is None


def test_adjacent_docstring_uses_physical_lines_across_multiline_comments() -> None:
    source = (
        "/-- A standalone explanation that spans\n"
        "multiple physical source lines without changing the locator. -/\n"
        "@[simp]\n"
        "theorem multiline : True := by trivial\n"
    )
    assert _adjacent_docstring(source, 4) == (
        " A standalone explanation that spans\n"
        "multiple physical source lines without changing the locator. "
    )


def test_library_nl_canary_rejects_raw_comment_delimiters() -> None:
    assert _strict_library_nl("A long mathematical statement /- crosses a boundary here.") is None
    assert _strict_library_nl("A long mathematical statement closes a boundary here. -/") is None


def test_frozen_92_row_docstring_corruption_impact_replays() -> None:
    fixture = json.loads(_CORRUPTION_FIXTURE.read_text(encoding="utf-8"))
    expected = fixture["rows"]
    assert len(expected) == fixture["expected_rows"] == 92
    assert Counter(row["release_class"] for row in expected) == {
        "library_mathlib": 54,
        "library_physlib": 32,
        "library_cslib": 6,
    }
    if not (_V1_BUNDLE / "sources.jsonl").is_file():
        pytest.skip("frozen private v1 source evidence is unavailable on this host")

    v1_rows = [
        json.loads(line)
        for line in (_V1_BUNDLE / "sources.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    observed = {
        row["source_id"]: row
        for row in v1_rows
        if "/-" in row["nl_statement"] or "-/" in row["nl_statement"]
    }
    expected_by_id = {row["v1_source_id"]: row for row in expected}
    assert set(observed) == set(expected_by_id)

    source_cache: dict[Path, str] = {}
    for source_id, impact in expected_by_id.items():
        old = observed[source_id]
        assert sha256_hex(old["nl_statement"].encode()) == impact["v1_nl_sha256"]
        source_path = Path(old["provenance"]["source_path"])
        source = source_cache.setdefault(source_path, source_path.read_text(encoding="utf-8"))
        line_number = int(impact["source_locator"].rsplit(":", 1)[1])
        raw = _adjacent_docstring(source, line_number)
        corrected = _strict_library_nl(raw) if raw is not None else None
        if impact["corrected_disposition"] == "recovered_clean_docstring":
            assert corrected is not None
            assert sha256_hex(corrected.encode()) == impact["corrected_nl_sha256"]
            assert "/-" not in corrected and "-/" not in corrected
        else:
            assert corrected is None


def _qualified(index: int, release_class: str, domain: str) -> QualifiedSource:
    nl = f"For every natural number n, the value n plus zero equals n in example {index}."
    proposition = f"∀ n : Nat, n + {index - index} = n"
    provenance = SourceProvenance(
        source_family="public_research",
        source_url="https://example.test/source",
        source_revision="revision",
        source_path=f"source-{index}.lean",
        source_file_sha256="0" * 64,
        manifest_path="manifest.json",
        manifest_sha256="1" * 64,
        source_recipe_sha256="2" * 64,
        license_card_value="Apache-2.0",
        redistribution_note="test",
        nl_extraction_rule="test",
        trusted_reference_basis="test",
    )
    theorem_id = f"test:{index}"
    record = SourceRecord(
        source_id=stable_id(
            "sft2b_source",
            {
                "reference_theorem_id": theorem_id,
                "nl_statement": nl,
                "source_revision": provenance.source_revision,
            },
        ),
        nl_statement=nl,
        reference_theorem_id=theorem_id,
        reference_declaration_name=f"t{index}",
        reference_proposition=proposition,
        reference_proposition_sha256=sha256_hex(proposition.encode()),
        compile_context=CompileContextRecord(
            source_context_id="ctx:" + "3" * 64,
            render_compile_context_id="ctx:" + "4" * 64,
            project_id="mathlib",
            project_revision="5" * 40,
            project_path="/tmp/mathlib",
            lean_version="v4.31.0",
            import_header="import Mathlib\n",
            source_context_path="context.json",
            source_context_sha256="6" * 64,
            helper_path="helper.lean",
            helper_sha256="7" * 64,
        ),
        provenance=provenance,
        standalone_nl=True,
        trusted_reference=True,
        training_eligible=True,
    )
    audited = AuditedSource(
        source_class=release_class,
        record=record,
        headless=f"(n : Nat) : n + 0 = n -- {index}",
        near_dup_hash=f"{index:064x}",
        problem_identity=f"test::{index}",
        selection_group=domain,
        selection_hash=f"{1000 - index:064x}",
        complexity_score=100,
    )
    return QualifiedSource(audited, release_class, "test", domain, "test")


def test_matched_view_is_deterministic_and_stratified() -> None:
    values = [
        *(_qualified(index, "a", "x") for index in range(1, 5)),
        *(_qualified(index, "b", "y") for index in range(5, 9)),
    ]
    selected = _matched_50k(values, 6)
    assert len(selected) == 6
    assert {value.release_class for value in selected} == {"a", "b"}
    assert [value.audited.record.source_id for value in selected] == [
        value.audited.record.source_id for value in _matched_50k(values, 6)
    ]


def test_workbook_discourse_audit_retains_claims_but_quarantines_answers() -> None:
    claim = r"Prove that $\boxed{x + 0 = x}$."
    answer = r"Solution: simplifying gives $x = \boxed{3}$."
    assert _workbook_discourse_flags(claim) == ("boxed_or_fboxed_answer",)
    assert _workbook_discourse_disposition(claim)[0] == "retain_explicit_claim_or_question"
    assert _workbook_discourse_flags(answer) == (
        "boxed_or_fboxed_answer",
        "solution_or_proof_header",
    )
    assert _workbook_discourse_disposition(answer)[0] == ("quarantine_solution_or_answer_discourse")


def test_semantic_sample_is_exact_per_class_and_deterministic() -> None:
    values = [
        *(_qualified(index, "a", "x") for index in range(1, 5)),
        *(_qualified(index, "b", "y") for index in range(5, 9)),
    ]
    first = _semantic_sample(values, seed="audit", count_per_release_class=2)
    second = _semantic_sample(values, seed="audit", count_per_release_class=2)
    assert tuple(first) == tuple(second)
    assert Counter(value.release_class for value in first.values()) == {"a": 2, "b": 2}
