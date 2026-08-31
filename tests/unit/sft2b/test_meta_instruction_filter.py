from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import cast

import pytest

from leanfaith.config.hashing import hash_file
from leanfaith.sft2b.meta_instruction_filter import (
    BASELINE_FILTER_VERSION,
    FILTER_VERSION,
    MetaInstructionRejected,
    build_v2_active_impact_fixture,
    build_v2_impact_fixture,
    detect_currently_identified_meta_instruction,
    detect_meta_instruction,
    require_meta_instruction_free,
    verify_v2_active_impact_fixture,
    verify_v2_impact_fixture,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_FIXTURE = _REPO_ROOT / "configs/sft2b/source_meta_instruction_impact_v1.json"
_ACTIVE_FIXTURE = _REPO_ROOT / "configs/sft2b/source_meta_instruction_impact_v2.json"
_V2_BUNDLE = Path(
    "/storage/milikic/leanfaith/value_first/sft2_autoformalizer_v1/"
    "source_inputs/reform_diverse_full_v2"
)


def test_explicit_translation_and_direct_output_are_fail_closed() -> None:
    text = (
        "Translate the text above into English, retain its format, and output "
        "the translation result directly."
    )
    detection = detect_meta_instruction(text)
    assert detection is not None
    assert detection.filter_version == FILTER_VERSION
    assert detection.rule_ids == (
        "direct_translation_proximity_v1",
        "explicit_translate_text_english_v1",
    )
    assert detection.flags == (
        "direct_output_instruction",
        "explicit_translation_instruction",
    )
    with pytest.raises(MetaInstructionRejected, match="direct_translation_proximity_v1"):
        require_meta_instruction_free(text)


def test_retain_format_and_response_artifacts_are_independent_rules() -> None:
    retained = "保留源文本的换行和格式。"
    retain_detection = detect_meta_instruction(retained)
    assert retain_detection is not None
    assert retain_detection.rule_ids == ("retain_source_format_zh_v1",)
    assert retain_detection.flags == ("retain_format_instruction",)

    response = "The translation is provided as requested, maintaining the original structure."
    response_detection = detect_meta_instruction(response)
    assert response_detection is not None
    assert response_detection.rule_ids == (
        "format_preservation_boilerplate_v1",
        "translation_provided_response_v1",
        "translation_response_boilerplate_v1",
    )
    assert response_detection.flags == (
        "retain_format_instruction",
        "translation_response_meta",
    )


def test_frozen_baseline_detector_remains_the_exact_prior_known_contract() -> None:
    text = (
        "Translate the text above into English, retain its format, and output "
        "the translation result directly."
    )
    detection = detect_currently_identified_meta_instruction(text)
    assert detection is not None
    assert detection.filter_version == BASELINE_FILTER_VERSION
    assert detection.rule_ids == ("direct_translation_proximity_v1",)


@pytest.mark.parametrize(
    ("text", "expected_rule"),
    [
        ("The untranslated text is below.", "untranslated_fragment_v1"),
        ("Please translate the following text into English.", "explicit_translate_text_english_v1"),
        ("The provided text has been translated into English.", "text_translated_english_v1"),
        ("The translation is complete as requested.", "translation_response_boilerplate_v1"),
        (
            "The format and line breaks have been preserved as requested.",
            "format_preservation_boilerplate_v1",
        ),
        ("Here is the translation.", "here_is_translation_v1"),
        ("Translation: Solve the equation.", "translation_label_duplicate_v1"),
        (
            "The translation is identical to the original.",
            "translation_equivalence_note_v1",
        ),
        ("No translation is needed.", "translation_status_boilerplate_v1"),
        (
            "Will the translated text be used for further discussion?",
            "translated_text_followup_v1",
        ),
        (
            "There is a formatting issue in the original text. The corrected version follows.",
            "source_text_correction_note_v1",
        ),
        (
            "保留了原文的换行和格式。",
            "retain_source_format_zh_variants_v1",
        ),
        ("直接输出翻译结果。", "translation_response_zh_v1"),
        (
            "Note: The original problem seems to have a typo or misinterpretation.",
            "source_text_editorial_note_v1",
        ),
        (
            "No change needed as the text is already in English.",
            "english_text_no_change_response_v1",
        ),
    ],
)
def test_each_additive_rule_has_a_direct_regression(text: str, expected_rule: str) -> None:
    detection = detect_meta_instruction(text)
    assert detection is not None
    assert detection.rule_ids == (expected_rule,)


@pytest.mark.parametrize(
    "text",
    [
        "Translation-invariant measures assign the same mass after every group action.",
        "The linear transformation preserves the output of the machine.",
        "Convert the equation to slope-intercept form and prove the result directly.",
        "An order isomorphism preserves subtraction provided that it preserves addition.",
        "Translate the graph of y = sin x by the vector (1, 2).",
        "Translating a function by left multiplication preserves its integral.",
        (
            "Janica and Jelica need 30 hours to translate a certain number of pages; "
            "how long do three workers need?"
        ),
        (
            "A conversion rule divides kilolunes by four; this translates this way "
            "into a percentage error."
        ),
    ],
)
def test_mathematical_translation_format_and_output_language_is_not_rejected(text: str) -> None:
    assert detect_meta_instruction(text) is None
    require_meta_instruction_free(text)


def test_frozen_v2_impact_is_exactly_326_with_required_view_split() -> None:
    assert hash_file(_FIXTURE) == "8dc3e66023d687405bb77e4e811a2eea4dc79b4846e534db0d1afbbfd2604c25"
    if not (_V2_BUNDLE / "sources.jsonl").is_file():
        pytest.skip("frozen private v2 source evidence is unavailable on this host")
    verify_v2_impact_fixture(_V2_BUNDLE, _FIXTURE)
    fixture = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    assert fixture["expected_rows"] == len(fixture["rows"]) == 326
    assert fixture["expected_view_counts"] == {"legacy_tail": 64, "matched_core": 262}
    assert fixture["expected_release_class_counts"] == {
        "numina_current_auto": 15,
        "numina_current_human": 63,
        "numina_legacy_owner": 248,
    }
    assert [row["source_id"] for row in fixture["rows"]] == sorted(
        row["source_id"] for row in fixture["rows"]
    )
    assert len({row["source_id"] for row in fixture["rows"]}) == 326


def test_active_v2_impact_and_provisional_extension_counts_are_frozen() -> None:
    assert hash_file(_ACTIVE_FIXTURE) == (
        "44566540c96adc0ab96ca6aa4a8e8ae757edcc75a863fe5524fbd48689ee50ab"
    )
    fixture = json.loads(_ACTIVE_FIXTURE.read_text(encoding="utf-8"))
    assert fixture["expected_rows"] == len(fixture["rows"]) == 469
    assert fixture["expected_view_counts"] == {"legacy_tail": 75, "matched_core": 394}
    assert fixture["expected_release_class_counts"] == {
        "numina_current_auto": 29,
        "numina_current_human": 90,
        "numina_legacy_owner": 350,
    }
    additional = fixture["additional_impact"]
    assert additional["expected_rows"] == 143
    assert additional["expected_view_counts"] == {"legacy_tail": 11, "matched_core": 132}
    assert additional["expected_release_class_counts"] == {
        "numina_current_auto": 14,
        "numina_current_human": 27,
        "numina_legacy_owner": 102,
    }
    assert additional["incremental_rule_counts"] == {
        "explicit_translate_text_english_v1": 1,
        "format_preservation_boilerplate_v1": 11,
        "here_is_translation_v1": 5,
        "source_text_correction_note_v1": 1,
        "retain_source_format_zh_variants_v1": 17,
        "translation_response_zh_v1": 4,
        "source_text_editorial_note_v1": 4,
        "english_text_no_change_response_v1": 1,
        "text_translated_english_v1": 15,
        "translated_text_followup_v1": 1,
        "translation_equivalence_note_v1": 4,
        "translation_label_duplicate_v1": 6,
        "translation_response_boilerplate_v1": 33,
        "translation_status_boilerplate_v1": 2,
        "untranslated_fragment_v1": 38,
    }
    provisional = fixture["provisional_extension_receipt"]
    assert provisional["expected_rows"] == 79
    assert provisional["expected_view_counts"] == {"legacy_tail": 6, "matched_core": 73}
    assert fixture["additional_beyond_provisional"]["expected_rows"] == 64
    assert fixture["additional_beyond_provisional"]["expected_view_counts"] == {
        "legacy_tail": 5,
        "matched_core": 59,
    }


def test_remaining_translat_rows_are_explicit_non_human_negative_guards() -> None:
    fixture = json.loads(_ACTIVE_FIXTURE.read_text(encoding="utf-8"))
    review = fixture["remaining_translat_lexical_review"]
    assert review["expected_rows"] == len(review["rows"]) == 42
    assert review["expected_category_counts"] == {
        "mathematical_translation_operation": 39,
        "translation_workload_word_problem": 1,
        "unit_conversion_word_problem_language": 2,
    }
    assert review["expected_view_counts"] == {"legacy_tail": 1, "matched_core": 41}
    assert review["semantic_audit"] is False
    assert review["human_audit"] is False
    assert review["satisfies_human_review_contract"] is False
    assert all(row["satisfies_human_review_contract"] is False for row in review["rows"])
    assert len({row["source_id"] for row in review["rows"]}) == 42


def test_active_v2_fixture_replays_from_frozen_evidence() -> None:
    if not (_V2_BUNDLE / "sources.jsonl").is_file():
        pytest.skip("frozen private v2 source evidence is unavailable on this host")
    verify_v2_active_impact_fixture(_V2_BUNDLE, _FIXTURE, _ACTIVE_FIXTURE)
    observed = build_v2_active_impact_fixture(_V2_BUNDLE, _FIXTURE)
    assert observed == json.loads(_ACTIVE_FIXTURE.read_text(encoding="utf-8"))


def test_atomic_rule_exclusive_and_overlap_counts_are_frozen() -> None:
    if not (_V2_BUNDLE / "sources.jsonl").is_file():
        pytest.skip("frozen private v2 source evidence is unavailable on this host")
    fixture = build_v2_impact_fixture(_V2_BUNDLE)
    rows = cast(list[dict[str, object]], fixture["rows"])
    combinations = Counter("+".join(cast(list[str], row["rule_ids"])) for row in rows)
    assert combinations == {
        "direct_translation_proximity_v1": 246,
        "retain_source_format_zh_v1": 52,
        "translation_provided_response_v1": 15,
        "direct_translation_proximity_v1+translation_provided_response_v1": 8,
        "retain_source_format_zh_v1+translation_provided_response_v1": 4,
        "direct_translation_proximity_v1+retain_source_format_zh_v1": 1,
    }
