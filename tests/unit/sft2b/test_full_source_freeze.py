from __future__ import annotations

from leanfaith.config.hashing import sha256_hex
from leanfaith.sft2b.full_source_freeze import (
    QualifiedSource,
    _adjacent_docstring,
    _has_denied_marker,
    _matched_50k,
    _render_source_prompt,
    _strict_library_nl,
)
from leanfaith.sft2b.pilot_source_freeze import AuditedSource
from leanfaith.sft2b.schemas import CompileContextRecord, SourceProvenance, SourceRecord, stable_id


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
