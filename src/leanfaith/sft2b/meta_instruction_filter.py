"""Versioned fail-closed detection for confirmed source meta-instructions.

The v2 source release contains a narrow, reproducible family of translation
prompt/response artifacts.  The primary rules below intentionally operate on
``nl_statement`` only.  They do not inspect provenance, release membership, or
the trusted Lean reference, so a disposition cannot be manufactured from a
source class or expected view.

This module freezes the confirmed v2 regression; it is not an assertion that
all possible prompt contamination can be discovered lexically.  New rules
must be additive and backed by reviewed examples.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from leanfaith.config.hashing import hash_file, sha256_hex

BASELINE_FILTER_VERSION: Literal["sft2b_source_meta_instruction_filter_v1"] = (
    "sft2b_source_meta_instruction_filter_v1"
)
FILTER_VERSION: Literal["sft2b_source_meta_instruction_filter_v2"] = (
    "sft2b_source_meta_instruction_filter_v2"
)
BASELINE_IMPACT_SCHEMA_VERSION = "sft2b_source_meta_instruction_impact_v1"
ACTIVE_IMPACT_SCHEMA_VERSION = "sft2b_source_meta_instruction_active_impact_v2"
QUARANTINE_DISPOSITION: Literal["quarantine_meta_instruction"] = "quarantine_meta_instruction"

MetaInstructionRuleId = Literal[
    "direct_translation_proximity_v1",
    "english_text_no_change_response_v1",
    "explicit_translate_text_english_v1",
    "format_preservation_boilerplate_v1",
    "here_is_translation_v1",
    "retain_source_format_zh_variants_v1",
    "retain_source_format_zh_v1",
    "source_text_correction_note_v1",
    "source_text_editorial_note_v1",
    "text_translated_english_v1",
    "translated_text_followup_v1",
    "translation_equivalence_note_v1",
    "translation_label_duplicate_v1",
    "translation_provided_response_v1",
    "translation_response_boilerplate_v1",
    "translation_status_boilerplate_v1",
    "translation_response_zh_v1",
    "untranslated_fragment_v1",
]
MetaInstructionFlag = Literal[
    "direct_output_instruction",
    "explicit_translation_instruction",
    "retain_format_instruction",
    "source_correction_meta",
    "translation_response_meta",
]

# A direct-output translation instruction or response.  The bounded window is
# deliberate: it catches the repeated source corruption without treating
# mathematical statements about translations, outputs, or direct maps as meta.
_DIRECT_TRANSLATION = re.compile(
    r"\bdirectly\b.{0,50}\btranslat\w*\b|"
    r"\btranslat\w*\b.{0,50}\bdirectly\b",
    re.IGNORECASE | re.DOTALL,
)

# Literal fragment of the repeated Chinese instruction "retain the source
# text's line breaks and format".  Matching the complete fragment avoids
# language/script detection and remains stable across surrounding translations.
_RETAIN_SOURCE_FORMAT_ZH = "保留源文本"

# Response-side leakage such as "The translation is provided as requested".
# This is kept separate from direct-output instructions because these rows may
# contain only the generated attestation, not the original request.
_TRANSLATION_PROVIDED = re.compile(
    r"\btranslation\b.{0,100}\bprovid\w*\b",
    re.IGNORECASE | re.DOTALL,
)

# Additive v2 rules were derived from a complete lexical inspection of the
# remaining v2 rows containing ``translat*``.  They require prompt/response
# vocabulary (text, English, formatting, requested, or duplicate labels), not
# the bare mathematical word "translation".
_UNTRANSLATED_FRAGMENT = re.compile(
    r"\buntranslated\s+(?:text|part|portion)\b",
    re.IGNORECASE,
)
_EXPLICIT_TRANSLATE_TEXT_ENGLISH = re.compile(
    r"\btranslate\b.{0,80}\b(?:the\s+)?"
    r"(?:(?:above|following|given|provided|source|original)\s+)?text\b"
    r".{0,100}\b(?:into\s+)?english\b|"
    r"\btranslate\s+the\s+text\s+above\b.{0,100}\b(?:into\s+)?english\b",
    re.IGNORECASE | re.DOTALL,
)
_TEXT_TRANSLATED_ENGLISH = re.compile(
    r"\b(?:the\s+)?(?:(?:above|following|given|provided|source|original)\s+)?"
    r"text\b.{0,80}\b(?:has\s+been|is|was)\s+translated\s+into\s+english\b",
    re.IGNORECASE | re.DOTALL,
)
_TRANSLATION_RESPONSE_BOILERPLATE = re.compile(
    r"\btranslation\b.{0,120}\b"
    r"(?:complete|completed|requested|maintain\w*|preserv\w*|retain\w*)\b|"
    r"\btranslated\s+(?:as|while)\s+requested\b",
    re.IGNORECASE | re.DOTALL,
)
_FORMAT_PRESERVATION_BOILERPLATE = re.compile(
    r"\b(?:retain|keep|preserv\w*|maintain\w*)\b.{0,120}\b"
    r"(?:original|source)\s+(?:text|format(?:ting)?|line\s*breaks?|structure)\b|"
    r"\b(?:original|source)\s+(?:text|format(?:ting)?|line\s*breaks?|structure)\b"
    r".{0,120}\b(?:retain|keep|preserv\w*|maintain\w*)\b|"
    r"\b(?:format(?:ting)?|line\s*breaks?)\b.{0,100}\b"
    r"(?:preserv\w*|maintain\w*|retain\w*)\b.{0,80}\brequested\b|"
    r"\b(?:preserv\w*|maintain\w*|retain\w*)\b.{0,100}\b"
    r"(?:format(?:ting)?|line\s*breaks?)\b.{0,80}\brequested\b",
    re.IGNORECASE | re.DOTALL,
)
_HERE_IS_TRANSLATION = re.compile(
    r"\bhere\s+(?:is|are)\s+(?:the\s+)?(?:translation|translated\s+text)\b|"
    r"\bhere's\s+(?:the\s+)?translation\b",
    re.IGNORECASE,
)
_TRANSLATION_LABEL_DUPLICATE = re.compile(
    r"(?:^|[\s(\-])translation\s*:\s*|"
    r"\btranslating\s+(?:the\s+)?(?:above\s+)?"
    r"(?:text|problem statement)\s+into\s+english\s*[:,]",
    re.IGNORECASE,
)
_TRANSLATION_EQUIVALENCE_NOTE = re.compile(
    r"\btranslation\s+(?:is|was)\s+(?:the\s+)?(?:same|identical)\b|"
    r"\btranslation\s+is\s+identical\s+to\b",
    re.IGNORECASE,
)
_TRANSLATION_STATUS_BOILERPLATE = re.compile(
    r"\bfresh\s+translation\b|\bno\s+translation\s+is\s+needed\b",
    re.IGNORECASE,
)
_TRANSLATED_TEXT_FOLLOWUP = re.compile(
    r"\btranslated\s+text\b.{0,140}\b"
    r"(?:further\s+discussion|need\s+the\s+solution|used\s+for)\b",
    re.IGNORECASE | re.DOTALL,
)
_SOURCE_TEXT_CORRECTION_NOTE = re.compile(
    r"\bformatting\s+issue\s+in\s+the\s+(?:original|source)\s+text\b"
    r".{0,200}\bcorrected\s+version\b",
    re.IGNORECASE | re.DOTALL,
)

# The original 326-row regression contains the exact literal ``保留源文本``.
# A complete non-English pass found inflected response-side variants such as
# ``保留了原文的换行和格式`` and ``保持了源文本的换行和格式``.  Keep this
# additive so the exact v1 baseline remains replayable byte-for-byte.
_RETAIN_SOURCE_FORMAT_ZH_VARIANTS = re.compile(
    r"(?:保留|保持)了?(?:源文本|原文)的?(?:换行和格式|格式和换行|换行|格式)"
)

# Response leakage that contains the Chinese translation-result label but no
# English ``translat*`` token.  This includes direct-output instructions and
# duplicated translation-result sections.
_TRANSLATION_RESPONSE_ZH = re.compile(
    r"(?:直接输出)?翻译结果(?:如下|如上)?|"
    r"(?:正确的)?翻译结果|"
    r"翻译为"
)

# Explicit translator/editor commentary about damaged or retained source text.
# The bounded note/phrase vocabulary avoids treating ordinary mathematical
# remarks as source metadata.
_SOURCE_TEXT_EDITORIAL_NOTE = re.compile(
    r"\b(?:note|remark)\s*:.{0,500}\b"
    r"(?:original\s+(?:problem|text)|source\s+text|typo|misinterpretation|"
    r"incorrect\s+input|left\s+as|kept\s+in|formalized\s+question)\b|"
    r"\bi\s+(?:have\s+)?interpreted\b.{0,300}\b"
    r"(?:incorrect\s+input|based\s+on\s+the\s+context)\b",
    re.IGNORECASE | re.DOTALL,
)

_ENGLISH_TEXT_NO_CHANGE_RESPONSE = re.compile(
    r"\bno\s+change\s+(?:is\s+)?needed\b.{0,160}\b"
    r"(?:text\s+is\s+already\s+in\s+english|already\s+in\s+english)\b",
    re.IGNORECASE | re.DOTALL,
)

# This exact two-rule pass reproduces the provisional 79-row extension.  It is
# retained as an intermediate receipt even though the active filter is wider.
_PROVISIONAL_ORIGINAL_TEXT_FORMAT = re.compile(
    r"(?:original|source) text(?:'s)?.{0,100}(?:line\s*breaks?|format)|"
    r"(?:line\s*breaks?|format).{0,100}(?:original|source) text",
    re.IGNORECASE | re.DOTALL,
)
_TRANSLAT_TOKEN = re.compile(r"\btranslat\w*\b", re.IGNORECASE)

_ADDITIVE_RULE_ORDER: tuple[MetaInstructionRuleId, ...] = (
    "untranslated_fragment_v1",
    "explicit_translate_text_english_v1",
    "text_translated_english_v1",
    "translation_response_boilerplate_v1",
    "format_preservation_boilerplate_v1",
    "here_is_translation_v1",
    "translation_label_duplicate_v1",
    "translation_equivalence_note_v1",
    "translation_status_boilerplate_v1",
    "translated_text_followup_v1",
    "source_text_correction_note_v1",
    "retain_source_format_zh_variants_v1",
    "translation_response_zh_v1",
    "source_text_editorial_note_v1",
    "english_text_no_change_response_v1",
)

# Complete row-by-row Codex development inspection of lexical negatives after
# the active rules are applied to every v2 ``translat*`` row.  This is
# deliberately an explicit ID allowlist rather than a heuristic classifier.
# It is a filter regression, not a semantic audit and not a human-review claim.
_REVIEWED_NON_META_TRANSLAT_IDS = frozenset(
    {
        "sft2b_source:0af9d2994c020bf596fcaad91114acf256dd643b76d7ad06ece1d4215b1587fe",
        "sft2b_source:0bbf7cc62d11cab3c96af551660aba259e5a311be81abd2472f3db7c592f49e1",
        "sft2b_source:0e6bce055ce710c7d005dc788ce009f666cb21087b9337b98d54b26d316713a3",
        "sft2b_source:135ff0a5d7acaa18a66d4219a351d9474ce5bae11ee668677f85b4ebcf45a7a3",
        "sft2b_source:177db9c9a32358ecdc21122076af83d9d8e538858b7f2ed871cb05c47848a7a0",
        "sft2b_source:23baa0ab22929d347dda356f289719c04d70a588a1526e1516a82793d9502b2b",
        "sft2b_source:2f096d9274b37375b0b211c6e7d5ba3d36e480b0db6d894a0a0c7c2f3088bc4e",
        "sft2b_source:31fcb2f6bef3b0b16597b33377108c62b2b0db6b27160e06c007680d328a944a",
        "sft2b_source:376e4f4a867125b8135c96b111ec11c562745b5c3f90b7ea6bad05d877697be1",
        "sft2b_source:38b7854b5e47a7831a5c2beb13cc6b00edce886d0baaf496a7dd3019c4efee5e",
        "sft2b_source:38cdabea40ffdc51ddd192c87d88c660a53dbe191288d55238e80423629f031f",
        "sft2b_source:3a7611e20573d79fa1e287152c361588f4056f72bc90f6b11da69837e12f2753",
        "sft2b_source:3e50f962c92c831afcd173fd0ca593c7fd67071b9148a4c002bb6d9ed8e17118",
        "sft2b_source:4cfb5c1a90b2403b38dc4d99e4aff41ccb824802b1cabd626fe61a35c992fc5f",
        "sft2b_source:5586bebabdaff0148db59dd9dbe8597df6afd6b4b0b42cd89e2653e7fc91e6d5",
        "sft2b_source:5e427ff22fdb7a2104613b942259028c268f95f4eb05aded9f8af65bd7e9e8eb",
        "sft2b_source:66708ff8df8cbb3604c984aa8770add0330d5a6b6b1aa624fa773e627385818b",
        "sft2b_source:69fe33a9f2fdd7f882bf8f3634a6b664242d2ea41b95702a166c8e90ea799637",
        "sft2b_source:7175dd4bb5468bac9d2eca27c0333e8e8628cb0811d1c916372ce0a4d591c982",
        "sft2b_source:734a1821c5ff9486896591d6a2395e0d5abd0e5f9e494dbd7efb8c4e03134ab7",
        "sft2b_source:78a6f8885932436524f813f1291edb6fc25261044530520763095b960f202abf",
        "sft2b_source:79f3ef0c98686d24c74c171a880f42f35946d810b17c0198516ef464662f86ae",
        "sft2b_source:7bb828f5a15b79497abe90fee9a16b893febae720d3a3d9ddb715fc56d2471bc",
        "sft2b_source:854b6a8dcc5c97529051deb464811a58fba2760bf95faf3b8478c08343f665cb",
        "sft2b_source:898240c5b0b152bafa6000cd1b2b885676e2dee37ba3135abbd265b5b28aebee",
        "sft2b_source:918e6b482b7875a63c6bb7245c14e685ed3b4df42a80080a64ecd062d6a47445",
        "sft2b_source:ae583aaabbcf596d3c1bd5defc339fccb979970f3d7b12d3a3ea9d0a47e8059b",
        "sft2b_source:b11e32dbcf5adb285c4f4e90b6d9bc5414843770ce9b43ca89798bb60c01cec8",
        "sft2b_source:b4ff01f01f7d1d7b1d19b85e4979978fc750023674667455e2617e64f02bef14",
        "sft2b_source:b961797e244ce913793174f7eb65f1e041247a55fae48fd2f447c0d25f6fffdd",
        "sft2b_source:c1029122260377013637f58edde6a77ac905666c8aad594e87c48e4518ea6581",
        "sft2b_source:c62e283a85399498768c7381ff55afa88cc7bf6e26b60ad43fd12e2288bc4e99",
        "sft2b_source:ca6bb0740d9deb3a59240d6ca12f02d7a009bd8ce2a903a53487b95792d762d7",
        "sft2b_source:d0a8207cb467115ce3b955e2b83e7285fb1aa44f9285ff6f270e5a5e6bdbbb5b",
        "sft2b_source:d546c0e83710b1a3f17b85151809acd6f26b6d96fccda37c3d66129537b879b1",
        "sft2b_source:dec75a3fc916c41d108a7162f93b95a6433522241ff174d939e39f77524e293d",
        "sft2b_source:e6ea687f7740441ee0cb7511547cb00672eefe843d7bea2c11add872527649ba",
        "sft2b_source:e9a6bb7d5b7d30b977a5e12e9aa06c4d124f9710dd5b701463d24bacfa21411b",
        "sft2b_source:ec170ab384be5a76bc0c57f0bb2945088aaf6fc2b59a93fb1eadfd3378b544f8",
        "sft2b_source:ed7b55443a1cab445a36c738c97797fdf7ed429e9bf00fabd4329de1e11d4394",
        "sft2b_source:f859403a4c6ee2f0e41761f74c5345b2969ee64121e6fce7546985645496ade7",
        "sft2b_source:f9fab9f44215e351da08876e912baae5745b512da86a1cb185b0339804d5d794",
    }
)
_UNIT_CONVERSION_TRANSLAT_IDS = frozenset(
    {
        "sft2b_source:0e6bce055ce710c7d005dc788ce009f666cb21087b9337b98d54b26d316713a3",
        "sft2b_source:79f3ef0c98686d24c74c171a880f42f35946d810b17c0198516ef464662f86ae",
    }
)
_TRANSLATION_WORKLOAD_IDS = frozenset(
    {"sft2b_source:0af9d2994c020bf596fcaad91114acf256dd643b76d7ad06ece1d4215b1587fe"}
)


@dataclass(frozen=True, slots=True)
class MetaInstructionDetection:
    """A deterministic quarantine decision with all matched atomic rules."""

    filter_version: Literal[
        "sft2b_source_meta_instruction_filter_v1",
        "sft2b_source_meta_instruction_filter_v2",
    ]
    rule_ids: tuple[MetaInstructionRuleId, ...]
    flags: tuple[MetaInstructionFlag, ...]
    disposition: Literal["quarantine_meta_instruction"]


class MetaInstructionRejected(ValueError):
    """Raised when a caller requires model-facing NL to pass the filter."""


def detect_currently_identified_meta_instruction(
    nl_statement: str,
) -> MetaInstructionDetection | None:
    """Replay only the frozen, prior-known 326-row v1 baseline.

    Empty strings are rejected by the source schema elsewhere.  This function
    treats only the three reviewed v2 atomic rules as terminal contamination.
    Rule and flag tuples are sorted so their serialization is deterministic.
    """

    rules: list[MetaInstructionRuleId] = []
    flags: set[MetaInstructionFlag] = set()
    if _DIRECT_TRANSLATION.search(nl_statement):
        rules.append("direct_translation_proximity_v1")
        flags.update(("direct_output_instruction", "explicit_translation_instruction"))
    if _RETAIN_SOURCE_FORMAT_ZH in nl_statement:
        rules.append("retain_source_format_zh_v1")
        flags.add("retain_format_instruction")
    if _TRANSLATION_PROVIDED.search(nl_statement):
        rules.append("translation_provided_response_v1")
        flags.add("translation_response_meta")
    if not rules:
        return None
    return MetaInstructionDetection(
        filter_version=BASELINE_FILTER_VERSION,
        rule_ids=tuple(sorted(rules)),
        flags=tuple(sorted(flags)),
        disposition=QUARANTINE_DISPOSITION,
    )


def _additive_rule_ids(nl_statement: str) -> tuple[MetaInstructionRuleId, ...]:
    rules: list[MetaInstructionRuleId] = []
    if _UNTRANSLATED_FRAGMENT.search(nl_statement):
        rules.append("untranslated_fragment_v1")
    if _EXPLICIT_TRANSLATE_TEXT_ENGLISH.search(nl_statement):
        rules.append("explicit_translate_text_english_v1")
    if _TEXT_TRANSLATED_ENGLISH.search(nl_statement):
        rules.append("text_translated_english_v1")
    if _TRANSLATION_RESPONSE_BOILERPLATE.search(nl_statement):
        rules.append("translation_response_boilerplate_v1")
    if _FORMAT_PRESERVATION_BOILERPLATE.search(nl_statement):
        rules.append("format_preservation_boilerplate_v1")
    if _HERE_IS_TRANSLATION.search(nl_statement):
        rules.append("here_is_translation_v1")
    if _TRANSLATION_LABEL_DUPLICATE.search(nl_statement):
        rules.append("translation_label_duplicate_v1")
    if _TRANSLATION_EQUIVALENCE_NOTE.search(nl_statement):
        rules.append("translation_equivalence_note_v1")
    if _TRANSLATION_STATUS_BOILERPLATE.search(nl_statement):
        rules.append("translation_status_boilerplate_v1")
    if _TRANSLATED_TEXT_FOLLOWUP.search(nl_statement):
        rules.append("translated_text_followup_v1")
    if _SOURCE_TEXT_CORRECTION_NOTE.search(nl_statement):
        rules.append("source_text_correction_note_v1")
    if (
        _RETAIN_SOURCE_FORMAT_ZH_VARIANTS.search(nl_statement)
        and _RETAIN_SOURCE_FORMAT_ZH not in nl_statement
    ):
        rules.append("retain_source_format_zh_variants_v1")
    if _TRANSLATION_RESPONSE_ZH.search(nl_statement):
        rules.append("translation_response_zh_v1")
    if _SOURCE_TEXT_EDITORIAL_NOTE.search(nl_statement):
        rules.append("source_text_editorial_note_v1")
    if _ENGLISH_TEXT_NO_CHANGE_RESPONSE.search(nl_statement):
        rules.append("english_text_no_change_response_v1")
    return tuple(rules)


def detect_meta_instruction(nl_statement: str) -> MetaInstructionDetection | None:
    """Return the active v2 fail-closed lexical quarantine decision."""

    baseline = detect_currently_identified_meta_instruction(nl_statement)
    rules = list(baseline.rule_ids) if baseline is not None else []
    flags = set(baseline.flags) if baseline is not None else set()
    additive_rules = _additive_rule_ids(nl_statement)
    rules.extend(additive_rules)
    if "explicit_translate_text_english_v1" in additive_rules:
        flags.add("explicit_translation_instruction")
    if "format_preservation_boilerplate_v1" in additive_rules:
        flags.add("retain_format_instruction")
    if "retain_source_format_zh_variants_v1" in additive_rules:
        flags.add("retain_format_instruction")
    if set(additive_rules) & {
        "source_text_correction_note_v1",
        "source_text_editorial_note_v1",
    }:
        flags.add("source_correction_meta")
    if set(additive_rules) & {
        "english_text_no_change_response_v1",
        "translation_response_zh_v1",
    }:
        flags.add("translation_response_meta")
        if "直接输出" in nl_statement:
            flags.add("direct_output_instruction")
    if set(additive_rules) - {
        "explicit_translate_text_english_v1",
        "format_preservation_boilerplate_v1",
        "source_text_correction_note_v1",
        "source_text_editorial_note_v1",
        "retain_source_format_zh_variants_v1",
        "translation_response_zh_v1",
        "english_text_no_change_response_v1",
    }:
        flags.add("translation_response_meta")
    if not rules:
        return None
    return MetaInstructionDetection(
        filter_version=FILTER_VERSION,
        rule_ids=tuple(sorted(set(rules))),
        flags=tuple(sorted(flags)),
        disposition=QUARANTINE_DISPOSITION,
    )


def require_meta_instruction_free(nl_statement: str) -> None:
    """Fail closed when confirmed source meta-instructions are present."""

    detection = detect_meta_instruction(nl_statement)
    if detection is not None:
        raise MetaInstructionRejected(
            "model-facing source contains confirmed meta-instruction text: "
            + ",".join(detection.rule_ids)
        )


def _json_object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return cast(dict[str, object], value)


def _jsonl(path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"expected JSON object at {path}:{line_number}")
            rows.append(cast(dict[str, object], value))
    return rows


def build_v2_impact_fixture(bundle_dir: Path) -> dict[str, object]:
    """Reproduce the frozen v2 impact set from source text and pinned views."""

    sources_path = bundle_dir / "sources.jsonl"
    audit_path = bundle_dir / "source_audit.jsonl"
    matched_path = bundle_dir / "matched_50000_source_ids.json"
    tail_path = bundle_dir / "legacy_tail_source_ids.json"
    source_rows = _jsonl(sources_path)
    audit_rows = _jsonl(audit_path)
    audit_by_id = {str(row["source_id"]): row for row in audit_rows}
    if len(audit_by_id) != len(audit_rows):
        raise ValueError("v2 source audit contains duplicate source IDs")

    matched_ids = {
        str(value) for value in cast(list[object], _json_object(matched_path)["source_ids"])
    }
    tail_ids = {str(value) for value in cast(list[object], _json_object(tail_path)["source_ids"])}
    if matched_ids & tail_ids:
        raise ValueError("v2 matched and tail views overlap")

    impact_rows: list[dict[str, object]] = []
    for source in source_rows:
        source_id = str(source["source_id"])
        nl_statement = str(source["nl_statement"])
        detection = detect_currently_identified_meta_instruction(nl_statement)
        if detection is None:
            continue
        if source_id in matched_ids:
            view = "matched_core"
        elif source_id in tail_ids:
            view = "legacy_tail"
        else:
            raise ValueError(f"impacted source is absent from both v2 views: {source_id}")
        audit = audit_by_id.get(source_id)
        if audit is None:
            raise ValueError(f"impacted source has no v2 source audit: {source_id}")
        impact_rows.append(
            {
                "disposition": detection.disposition,
                "flags": list(detection.flags),
                "nl_sha256": sha256_hex(nl_statement.encode("utf-8")),
                "reference_proposition_sha256": str(source["reference_proposition_sha256"]),
                "release_class": str(audit["release_class"]),
                "rule_ids": list(detection.rule_ids),
                "selection_hash": str(audit["selection_hash"]),
                "source_id": source_id,
                "v2_view": view,
            }
        )
    impact_rows.sort(key=lambda row: str(row["source_id"]))

    view_counts = Counter(str(row["v2_view"]) for row in impact_rows)
    class_counts = Counter(str(row["release_class"]) for row in impact_rows)
    rule_match_counts = Counter(
        str(rule_id) for row in impact_rows for rule_id in cast(list[object], row["rule_ids"])
    )
    rule_combination_counts = Counter(
        "+".join(str(value) for value in cast(list[object], row["rule_ids"])) for row in impact_rows
    )
    return {
        "schema_version": BASELINE_IMPACT_SCHEMA_VERSION,
        "filter_version": BASELINE_FILTER_VERSION,
        "derivation": {
            "input_field": "nl_statement",
            "source_or_provenance_dependent": False,
            "rules": [
                {
                    "rule_id": "direct_translation_proximity_v1",
                    "kind": "bounded_case_insensitive_regex",
                    "pattern": _DIRECT_TRANSLATION.pattern,
                },
                {
                    "rule_id": "retain_source_format_zh_v1",
                    "kind": "literal_utf8_substring",
                    "pattern": _RETAIN_SOURCE_FORMAT_ZH,
                },
                {
                    "rule_id": "translation_provided_response_v1",
                    "kind": "bounded_case_insensitive_regex",
                    "pattern": _TRANSLATION_PROVIDED.pattern,
                },
            ],
        },
        "v2_evidence": {
            "hf_repository": "Lemmy00/leanfaith-sft2-autoformalizer-v1",
            "hf_revision": "d0b961d2112d186009984242db674f2ad59905c7",
            "remote_prefix": "source_inputs/reform_diverse_full_v2",
            "files": {
                "legacy_tail_source_ids.json": hash_file(tail_path),
                "matched_50000_source_ids.json": hash_file(matched_path),
                "source_audit.jsonl": hash_file(audit_path),
                "sources.jsonl": hash_file(sources_path),
            },
        },
        "expected_rows": len(impact_rows),
        "expected_view_counts": dict(sorted(view_counts.items())),
        "expected_release_class_counts": dict(sorted(class_counts.items())),
        "expected_rule_match_counts": dict(sorted(rule_match_counts.items())),
        "expected_rule_combination_counts": dict(sorted(rule_combination_counts.items())),
        "rows": impact_rows,
    }


def verify_v2_impact_fixture(bundle_dir: Path, fixture_path: Path) -> None:
    """Fail unless the fixture exactly replays from the frozen v2 bundle."""

    expected = _json_object(fixture_path)
    observed = build_v2_impact_fixture(bundle_dir)
    if observed != expected:
        raise ValueError("meta-instruction impact fixture does not replay from frozen v2")
    if observed["expected_rows"] != 326:
        raise ValueError("meta-instruction v2 regression must contain exactly 326 rows")
    if observed["expected_view_counts"] != {"legacy_tail": 64, "matched_core": 262}:
        raise ValueError("meta-instruction v2 core/tail split is not 262/64")


def _active_rule_derivation() -> list[dict[str, object]]:
    return [
        {
            "rule_id": "direct_translation_proximity_v1",
            "kind": "bounded_case_insensitive_regex",
            "pattern": _DIRECT_TRANSLATION.pattern,
            "coverage": "currently_identified_baseline",
        },
        {
            "rule_id": "retain_source_format_zh_v1",
            "kind": "literal_utf8_substring",
            "pattern": _RETAIN_SOURCE_FORMAT_ZH,
            "coverage": "currently_identified_baseline",
        },
        {
            "rule_id": "translation_provided_response_v1",
            "kind": "bounded_case_insensitive_regex",
            "pattern": _TRANSLATION_PROVIDED.pattern,
            "coverage": "currently_identified_baseline",
        },
        {
            "rule_id": "untranslated_fragment_v1",
            "kind": "case_insensitive_regex",
            "pattern": _UNTRANSLATED_FRAGMENT.pattern,
            "coverage": "additional_lexical_finding",
        },
        {
            "rule_id": "explicit_translate_text_english_v1",
            "kind": "bounded_case_insensitive_regex",
            "pattern": _EXPLICIT_TRANSLATE_TEXT_ENGLISH.pattern,
            "coverage": "additional_lexical_finding",
        },
        {
            "rule_id": "text_translated_english_v1",
            "kind": "bounded_case_insensitive_regex",
            "pattern": _TEXT_TRANSLATED_ENGLISH.pattern,
            "coverage": "additional_lexical_finding",
        },
        {
            "rule_id": "translation_response_boilerplate_v1",
            "kind": "bounded_case_insensitive_regex",
            "pattern": _TRANSLATION_RESPONSE_BOILERPLATE.pattern,
            "coverage": "additional_lexical_finding",
        },
        {
            "rule_id": "format_preservation_boilerplate_v1",
            "kind": "bounded_case_insensitive_regex",
            "pattern": _FORMAT_PRESERVATION_BOILERPLATE.pattern,
            "coverage": "additional_lexical_finding",
        },
        {
            "rule_id": "here_is_translation_v1",
            "kind": "case_insensitive_regex",
            "pattern": _HERE_IS_TRANSLATION.pattern,
            "coverage": "additional_lexical_finding",
        },
        {
            "rule_id": "translation_label_duplicate_v1",
            "kind": "case_insensitive_regex",
            "pattern": _TRANSLATION_LABEL_DUPLICATE.pattern,
            "coverage": "additional_lexical_finding",
        },
        {
            "rule_id": "translation_equivalence_note_v1",
            "kind": "case_insensitive_regex",
            "pattern": _TRANSLATION_EQUIVALENCE_NOTE.pattern,
            "coverage": "additional_lexical_finding",
        },
        {
            "rule_id": "translation_status_boilerplate_v1",
            "kind": "case_insensitive_regex",
            "pattern": _TRANSLATION_STATUS_BOILERPLATE.pattern,
            "coverage": "additional_lexical_finding",
        },
        {
            "rule_id": "translated_text_followup_v1",
            "kind": "bounded_case_insensitive_regex",
            "pattern": _TRANSLATED_TEXT_FOLLOWUP.pattern,
            "coverage": "additional_lexical_finding",
        },
        {
            "rule_id": "source_text_correction_note_v1",
            "kind": "bounded_case_insensitive_regex",
            "pattern": _SOURCE_TEXT_CORRECTION_NOTE.pattern,
            "coverage": "additional_lexical_finding",
        },
        {
            "rule_id": "retain_source_format_zh_variants_v1",
            "kind": "utf8_regex",
            "pattern": _RETAIN_SOURCE_FORMAT_ZH_VARIANTS.pattern,
            "coverage": "additional_non_english_lexical_finding",
        },
        {
            "rule_id": "translation_response_zh_v1",
            "kind": "utf8_regex",
            "pattern": _TRANSLATION_RESPONSE_ZH.pattern,
            "coverage": "additional_non_english_lexical_finding",
        },
        {
            "rule_id": "source_text_editorial_note_v1",
            "kind": "bounded_case_insensitive_regex",
            "pattern": _SOURCE_TEXT_EDITORIAL_NOTE.pattern,
            "coverage": "additional_editorial_lexical_finding",
        },
        {
            "rule_id": "english_text_no_change_response_v1",
            "kind": "bounded_case_insensitive_regex",
            "pattern": _ENGLISH_TEXT_NO_CHANGE_RESPONSE.pattern,
            "coverage": "additional_translation_response_finding",
        },
    ]


def _reviewed_non_meta_translat_category(source_id: str) -> tuple[str, str]:
    if source_id in _UNIT_CONVERSION_TRANSLAT_IDS:
        return (
            "unit_conversion_word_problem_language",
            "The token is idiomatic prose inside a standalone unit-conversion problem, "
            "not a request or response about translating source text.",
        )
    if source_id in _TRANSLATION_WORKLOAD_IDS:
        return (
            "translation_workload_word_problem",
            "Translation is the work measured by a standalone rate problem, not an "
            "instruction to translate the source.",
        )
    return (
        "mathematical_translation_operation",
        "The token denotes a mathematical or geometric translation of a set, function, "
        "measure, curve, or graph, not a source-text instruction or response.",
    )


def build_v2_active_impact_fixture(
    bundle_dir: Path,
    baseline_fixture_path: Path,
) -> dict[str, object]:
    """Build the additive active-filter receipt without mutating v2 evidence."""

    verify_v2_impact_fixture(bundle_dir, baseline_fixture_path)
    baseline_fixture = _json_object(baseline_fixture_path)
    baseline_rows = cast(list[dict[str, object]], baseline_fixture["rows"])
    baseline_ids = {str(row["source_id"]) for row in baseline_rows}

    sources_path = bundle_dir / "sources.jsonl"
    audit_path = bundle_dir / "source_audit.jsonl"
    matched_path = bundle_dir / "matched_50000_source_ids.json"
    tail_path = bundle_dir / "legacy_tail_source_ids.json"
    source_rows = _jsonl(sources_path)
    audit_rows = _jsonl(audit_path)
    audit_by_id = {str(row["source_id"]): row for row in audit_rows}
    if len(audit_by_id) != len(audit_rows):
        raise ValueError("v2 source audit contains duplicate source IDs")

    matched_ids = {
        str(value) for value in cast(list[object], _json_object(matched_path)["source_ids"])
    }
    tail_ids = {str(value) for value in cast(list[object], _json_object(tail_path)["source_ids"])}
    if matched_ids & tail_ids:
        raise ValueError("v2 matched and tail views overlap")

    def view_for(source_id: str) -> str:
        if source_id in matched_ids:
            return "matched_core"
        if source_id in tail_ids:
            return "legacy_tail"
        raise ValueError(f"v2 source is absent from both frozen views: {source_id}")

    active_rows: list[dict[str, object]] = []
    provisional_additional_ids: set[str] = set()
    remaining_translat_rows: list[dict[str, object]] = []
    incremental_rule_counts: Counter[str] = Counter()
    incremental_rule_view_counts: dict[str, Counter[str]] = {
        rule_id: Counter() for rule_id in _ADDITIVE_RULE_ORDER
    }

    for source in source_rows:
        source_id = str(source["source_id"])
        nl_statement = str(source["nl_statement"])
        view = view_for(source_id)
        audit = audit_by_id.get(source_id)
        if audit is None:
            raise ValueError(f"v2 source has no source audit: {source_id}")
        detection = detect_meta_instruction(nl_statement)
        if detection is not None:
            coverage = (
                "currently_identified_baseline"
                if source_id in baseline_ids
                else "additional_lexical_finding"
            )
            active_rows.append(
                {
                    "coverage": coverage,
                    "disposition": detection.disposition,
                    "flags": list(detection.flags),
                    "nl_sha256": sha256_hex(nl_statement.encode("utf-8")),
                    "reference_proposition_sha256": str(source["reference_proposition_sha256"]),
                    "release_class": str(audit["release_class"]),
                    "rule_ids": list(detection.rule_ids),
                    "selection_hash": str(audit["selection_hash"]),
                    "source_id": source_id,
                    "v2_view": view,
                }
            )
            if source_id not in baseline_ids:
                additive_rules = _additive_rule_ids(nl_statement)
                first_rule = next(
                    rule_id for rule_id in _ADDITIVE_RULE_ORDER if rule_id in additive_rules
                )
                incremental_rule_counts[first_rule] += 1
                incremental_rule_view_counts[first_rule][view] += 1
            if source_id not in baseline_ids and (
                _UNTRANSLATED_FRAGMENT.search(nl_statement)
                or _PROVISIONAL_ORIGINAL_TEXT_FORMAT.search(nl_statement)
            ):
                provisional_additional_ids.add(source_id)
        elif _TRANSLAT_TOKEN.search(nl_statement):
            category, rationale = _reviewed_non_meta_translat_category(source_id)
            remaining_translat_rows.append(
                {
                    "category": category,
                    "human_audit": False,
                    "method": "row_by_row_codex_development_inspection_v1",
                    "nl_sha256": sha256_hex(nl_statement.encode("utf-8")),
                    "rationale": rationale,
                    "release_class": str(audit["release_class"]),
                    "review_scope": "meta_instruction_false_positive_guard_not_semantic_audit",
                    "reviewer_type": "codex_agent",
                    "satisfies_human_review_contract": False,
                    "source_id": source_id,
                    "v2_view": view,
                    "verdict": "retain_no_meta_instruction_pattern",
                }
            )

    active_rows.sort(key=lambda row: str(row["source_id"]))
    remaining_translat_rows.sort(key=lambda row: str(row["source_id"]))
    active_ids = {str(row["source_id"]) for row in active_rows}
    additional_ids = active_ids - baseline_ids
    if not baseline_ids <= active_ids:
        raise ValueError("active meta-instruction detector does not preserve the v1 baseline")
    if not provisional_additional_ids <= additional_ids:
        raise ValueError("provisional extension is not a subset of active additional findings")

    remaining_translat_ids = {str(row["source_id"]) for row in remaining_translat_rows}
    if remaining_translat_ids != _REVIEWED_NON_META_TRANSLAT_IDS:
        missing = sorted(_REVIEWED_NON_META_TRANSLAT_IDS - remaining_translat_ids)
        unexpected = sorted(remaining_translat_ids - _REVIEWED_NON_META_TRANSLAT_IDS)
        raise ValueError(
            "remaining translat* lexical-negative set changed; "
            f"missing={missing!r}, unexpected={unexpected!r}"
        )

    active_view_counts = Counter(str(row["v2_view"]) for row in active_rows)
    active_class_counts = Counter(str(row["release_class"]) for row in active_rows)
    active_rule_match_counts = Counter(
        str(rule_id) for row in active_rows for rule_id in cast(list[object], row["rule_ids"])
    )
    active_rule_combination_counts = Counter(
        "+".join(str(value) for value in cast(list[object], row["rule_ids"])) for row in active_rows
    )
    additional_rows = [
        row for row in active_rows if row["coverage"] == "additional_lexical_finding"
    ]
    additional_view_counts = Counter(str(row["v2_view"]) for row in additional_rows)
    additional_class_counts = Counter(str(row["release_class"]) for row in additional_rows)
    provisional_view_counts = Counter(
        view_for(source_id) for source_id in provisional_additional_ids
    )
    beyond_provisional_ids = additional_ids - provisional_additional_ids
    beyond_provisional_view_counts = Counter(
        view_for(source_id) for source_id in beyond_provisional_ids
    )
    remaining_category_counts = Counter(str(row["category"]) for row in remaining_translat_rows)
    remaining_view_counts = Counter(str(row["v2_view"]) for row in remaining_translat_rows)

    return {
        "schema_version": ACTIVE_IMPACT_SCHEMA_VERSION,
        "filter_version": FILTER_VERSION,
        "derivation": {
            "input_field": "nl_statement",
            "source_or_provenance_dependent": False,
            "rules": _active_rule_derivation(),
        },
        "v2_evidence": {
            "hf_repository": "Lemmy00/leanfaith-sft2-autoformalizer-v1",
            "hf_revision": "d0b961d2112d186009984242db674f2ad59905c7",
            "remote_prefix": "source_inputs/reform_diverse_full_v2",
            "files": {
                "legacy_tail_source_ids.json": hash_file(tail_path),
                "matched_50000_source_ids.json": hash_file(matched_path),
                "source_audit.jsonl": hash_file(audit_path),
                "sources.jsonl": hash_file(sources_path),
            },
        },
        "baseline_receipt": {
            "fixture_path": baseline_fixture_path.name,
            "fixture_sha256": hash_file(baseline_fixture_path),
            "filter_version": BASELINE_FILTER_VERSION,
            "expected_rows": len(baseline_ids),
            "expected_view_counts": baseline_fixture["expected_view_counts"],
        },
        "expected_rows": len(active_rows),
        "expected_view_counts": dict(sorted(active_view_counts.items())),
        "expected_release_class_counts": dict(sorted(active_class_counts.items())),
        "expected_rule_match_counts": dict(sorted(active_rule_match_counts.items())),
        "expected_rule_combination_counts": dict(sorted(active_rule_combination_counts.items())),
        "additional_impact": {
            "expected_rows": len(additional_rows),
            "expected_view_counts": dict(sorted(additional_view_counts.items())),
            "expected_release_class_counts": dict(sorted(additional_class_counts.items())),
            "incremental_rule_order": list(_ADDITIVE_RULE_ORDER),
            "incremental_rule_counts": {
                rule_id: incremental_rule_counts[rule_id] for rule_id in _ADDITIVE_RULE_ORDER
            },
            "incremental_rule_view_counts": {
                rule_id: dict(sorted(incremental_rule_view_counts[rule_id].items()))
                for rule_id in _ADDITIVE_RULE_ORDER
            },
        },
        "provisional_extension_receipt": {
            "definition": [
                {
                    "kind": "case_insensitive_regex",
                    "pattern": _UNTRANSLATED_FRAGMENT.pattern,
                },
                {
                    "kind": "bounded_case_insensitive_regex",
                    "pattern": _PROVISIONAL_ORIGINAL_TEXT_FORMAT.pattern,
                },
            ],
            "expected_rows": len(provisional_additional_ids),
            "expected_view_counts": dict(sorted(provisional_view_counts.items())),
            "source_ids": sorted(provisional_additional_ids),
        },
        "additional_beyond_provisional": {
            "expected_rows": len(beyond_provisional_ids),
            "expected_view_counts": dict(sorted(beyond_provisional_view_counts.items())),
            "source_ids": sorted(beyond_provisional_ids),
        },
        "remaining_translat_lexical_review": {
            "semantic_audit": False,
            "human_audit": False,
            "satisfies_human_review_contract": False,
            "expected_rows": len(remaining_translat_rows),
            "expected_category_counts": dict(sorted(remaining_category_counts.items())),
            "expected_view_counts": dict(sorted(remaining_view_counts.items())),
            "rows": remaining_translat_rows,
        },
        "rows": active_rows,
    }


def verify_v2_active_impact_fixture(
    bundle_dir: Path,
    baseline_fixture_path: Path,
    fixture_path: Path,
) -> None:
    """Fail unless the active receipt exactly replays against frozen v2."""

    expected = _json_object(fixture_path)
    observed = build_v2_active_impact_fixture(bundle_dir, baseline_fixture_path)
    if observed != expected:
        raise ValueError("active meta-instruction fixture does not replay from frozen v2")
    if observed["expected_rows"] != 469:
        raise ValueError("active v2 meta-instruction impact must contain exactly 469 rows")
    if observed["expected_view_counts"] != {"legacy_tail": 75, "matched_core": 394}:
        raise ValueError("active v2 meta-instruction core/tail split is not 394/75")
    additional = cast(dict[str, object], observed["additional_impact"])
    if additional["expected_rows"] != 143:
        raise ValueError("active filter must add exactly 143 rows beyond the 326 baseline")
    if additional["expected_view_counts"] != {"legacy_tail": 11, "matched_core": 132}:
        raise ValueError("additional active-filter core/tail split is not 132/11")
    provisional = cast(dict[str, object], observed["provisional_extension_receipt"])
    if provisional["expected_rows"] != 79:
        raise ValueError("provisional active-filter extension must contain exactly 79 rows")
    if provisional["expected_view_counts"] != {"legacy_tail": 6, "matched_core": 73}:
        raise ValueError("provisional active-filter core/tail split is not 73/6")
    remaining = cast(dict[str, object], observed["remaining_translat_lexical_review"])
    if remaining["expected_rows"] != 42:
        raise ValueError("remaining reviewed translat* lexical negatives must contain 42 rows")


def _write_fixture(bundle_dir: Path, output_path: Path) -> None:
    payload = build_v2_impact_fixture(bundle_dir)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_active_fixture(
    bundle_dir: Path,
    baseline_fixture_path: Path,
    output_path: Path,
) -> None:
    payload = build_v2_active_impact_fixture(bundle_dir, baseline_fixture_path)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--baseline-fixture", type=Path)
    parser.add_argument("--active", action="store_true")
    parser.add_argument("--write-fixture", action="store_true")
    args = parser.parse_args(argv)
    if args.active:
        if args.baseline_fixture is None:
            parser.error("--active requires --baseline-fixture")
        if args.write_fixture:
            _write_active_fixture(args.bundle_dir, args.baseline_fixture, args.fixture)
        else:
            verify_v2_active_impact_fixture(
                args.bundle_dir,
                args.baseline_fixture,
                args.fixture,
            )
    elif args.write_fixture:
        _write_fixture(args.bundle_dir, args.fixture)
    else:
        verify_v2_impact_fixture(args.bundle_dir, args.fixture)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
