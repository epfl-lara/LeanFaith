"""Deterministic LF-021 direct-autoformalization prompt and strict parser.

This module has no provider or network behavior.  The caller owns raw-response
artifact persistence and must store it before calling
``parse_direct_autoformalization_output``.  A successful parse intentionally
returns only the extracted proof-free declaration and its content hash.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal

from leanfaith.config.hashing import canonical_json_bytes, sha256_hex
from leanfaith.schemas.enums import NLTrust
from leanfaith.schemas.ids import PROBLEM_PREFIX, id_pattern

DIRECT_AUTOFORMALIZATION_TEMPLATE_ID = "direct_autoformalize"
DIRECT_AUTOFORMALIZATION_TEMPLATE_VERSION = "v1"
DEFAULT_DIRECT_AUTOFORMALIZATION_TEMPLATE = (
    Path(__file__).resolve().parents[3]
    / "prompts"
    / "autoformalizers"
    / "direct_autoformalize_v1.txt"
)

_TEMPLATE_HASH_TOKEN = "{{PROMPT_TEMPLATE_SHA256}}"
_PROBLEM_JSON_TOKEN = "{{PROBLEM_JSON}}"
_TEMPLATE_TOKEN = re.compile(r"\{\{[A-Z0-9_]+\}\}")
_FENCE_LINE = re.compile(r"^[ \t]*([`~]{3,})([^`~]*)[ \t]*$")
_DECLARATION_HEAD = re.compile(
    r"^(?P<kind>theorem|lemma)[ \t]+(?P<name>[^\s:({\[]+)",
    re.UNICODE,
)
_COMMAND_LINE = re.compile(
    r"^[ \t]*(?:"
    r"theorem|lemma|def|abbrev|opaque|example|axiom|instance|"
    r"structure|class|inductive|namespace|section|end|open|"
    r"import|set_option|variable|universe"
    r")\b",
    re.MULTILINE,
)
_PROOF_TOKEN = re.compile(r"(?<![\w'])\b(?:by|sorry|admit)\b", re.UNICODE)


class DirectPromptErrorCode(StrEnum):
    """Fail-closed direct-prompt validation errors."""

    INVALID_PROBLEM = "invalid_problem"
    PRIVATE_SOURCE = "private_source"
    UNTRUSTED_NL = "untrusted_nl"
    EXTERNAL_TRANSMISSION_FORBIDDEN = "external_transmission_forbidden"
    DENYLIST_NOT_CLEARED = "denylist_not_cleared"
    TEMPLATE_NOT_FOUND = "template_not_found"
    TEMPLATE_NOT_UTF8 = "template_not_utf8"
    TEMPLATE_CONTRACT = "template_contract"


class DirectPromptError(ValueError):
    """A deterministic prompt could not be rendered safely."""

    def __init__(self, code: DirectPromptErrorCode, detail: str) -> None:
        self.code = code
        super().__init__(f"{code.value}: {detail}")


class DirectOutputErrorCode(StrEnum):
    """Strict direct-autoformalization parser failures."""

    EMPTY_OUTPUT = "empty_output"
    MISSING_FENCE = "missing_fence"
    MALFORMED_FENCE = "malformed_fence"
    MULTIPLE_FENCES = "multiple_fences"
    WRONG_FENCE_LANGUAGE = "wrong_fence_language"
    EXTRA_TEXT = "extra_text"
    EMPTY_DECLARATION = "empty_declaration"
    COMMENTARY_IN_FENCE = "commentary_in_fence"
    UNSUPPORTED_DECLARATION = "unsupported_declaration"
    MULTIPLE_DECLARATIONS = "multiple_declarations"
    MISSING_TYPE = "missing_type"
    PROOF_BEARING_OUTPUT = "proof_bearing_output"


class DirectOutputParseError(ValueError):
    """A raw model response violates the versioned output contract."""

    def __init__(self, code: DirectOutputErrorCode, detail: str) -> None:
        self.code = code
        super().__init__(f"{code.value}: {detail}")


@dataclass(frozen=True, slots=True)
class PublicTrustedProblem:
    """Explicit eligibility and provenance for a public trusted-NL problem."""

    problem_record_id: str
    problem_id: str
    problem_group: str
    nl_statement: str
    nl_source_link: str
    nl_trust: NLTrust
    source_id: str
    source_revision: str
    source_license: str
    source_is_public: bool
    external_transmission_allowed: bool
    denylist_checked: bool
    denylist_hits: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RenderedDirectPrompt:
    """One deterministic prompt with both template and render bindings."""

    template_id: str
    template_version: str
    template_sha256: str
    render_sha256: str
    problem_record_id: str
    text: str


@dataclass(frozen=True, slots=True)
class ParsedLeanDeclaration:
    """The proof-free parsed payload; the raw response stays caller-owned."""

    declaration_kind: Literal["theorem", "lemma"]
    declaration_name: str
    statement: str
    statement_sha256: str


def _require_nonempty(field: str, value: str) -> None:
    if not value.strip() or "\x00" in value:
        raise DirectPromptError(
            DirectPromptErrorCode.INVALID_PROBLEM,
            f"{field} must be nonempty UTF-8 text without NUL bytes",
        )


def _validate_public_trusted_problem(problem: PublicTrustedProblem) -> None:
    required = {
        "problem_record_id": problem.problem_record_id,
        "problem_id": problem.problem_id,
        "problem_group": problem.problem_group,
        "nl_statement": problem.nl_statement,
        "nl_source_link": problem.nl_source_link,
        "source_id": problem.source_id,
        "source_revision": problem.source_revision,
        "source_license": problem.source_license,
    }
    for field, value in required.items():
        _require_nonempty(field, value)

    if re.fullmatch(id_pattern(PROBLEM_PREFIX), problem.problem_record_id) is None:
        raise DirectPromptError(
            DirectPromptErrorCode.INVALID_PROBLEM,
            "problem_record_id must be a canonical 'problem:' ID",
        )
    if problem.nl_trust is not NLTrust.TRUSTED:
        raise DirectPromptError(
            DirectPromptErrorCode.UNTRUSTED_NL,
            "direct real-output collection requires nl_trust=trusted",
        )
    if problem.source_is_public is not True:
        raise DirectPromptError(
            DirectPromptErrorCode.PRIVATE_SOURCE,
            "direct prompt rendering requires an explicitly public source",
        )
    if problem.external_transmission_allowed is not True:
        raise DirectPromptError(
            DirectPromptErrorCode.EXTERNAL_TRANSMISSION_FORBIDDEN,
            "problem provenance forbids external transmission",
        )
    if problem.denylist_checked is not True or problem.denylist_hits:
        raise DirectPromptError(
            DirectPromptErrorCode.DENYLIST_NOT_CLEARED,
            "problem must have a completed denylist check with zero hits",
        )


def _load_template(template_path: Path) -> tuple[str, str]:
    if not template_path.is_file():
        raise DirectPromptError(
            DirectPromptErrorCode.TEMPLATE_NOT_FOUND,
            f"template is not a regular file: {template_path}",
        )
    template_bytes = template_path.read_bytes()
    try:
        template = template_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DirectPromptError(
            DirectPromptErrorCode.TEMPLATE_NOT_UTF8,
            f"template is not valid UTF-8: {template_path}",
        ) from exc

    expected = {_TEMPLATE_HASH_TOKEN, _PROBLEM_JSON_TOKEN}
    observed = set(_TEMPLATE_TOKEN.findall(template))
    counts_ok = all(template.count(token) == 1 for token in expected)
    if observed != expected or not counts_ok:
        raise DirectPromptError(
            DirectPromptErrorCode.TEMPLATE_CONTRACT,
            "template must contain each required placeholder exactly once "
            "and no unknown placeholders",
        )
    return template, sha256_hex(template_bytes)


def render_direct_autoformalization_prompt(
    problem: PublicTrustedProblem,
    *,
    template_path: Path = DEFAULT_DIRECT_AUTOFORMALIZATION_TEMPLATE,
) -> RenderedDirectPrompt:
    """Render the canonical v1 prompt after fail-closed eligibility checks."""

    _validate_public_trusted_problem(problem)
    template, template_sha256 = _load_template(template_path)
    problem_payload = {
        "schema": "public_trusted_problem_v1",
        "problem_record_id": problem.problem_record_id,
        "problem_id": problem.problem_id,
        "problem_group": problem.problem_group,
        "nl_statement": problem.nl_statement,
        "nl_source_link": problem.nl_source_link,
        "nl_trust": problem.nl_trust.value,
        "source_id": problem.source_id,
        "source_revision": problem.source_revision,
        "source_license": problem.source_license,
        "source_is_public": problem.source_is_public,
        "external_transmission_allowed": problem.external_transmission_allowed,
        "denylist_checked": problem.denylist_checked,
        "denylist_hits": list(problem.denylist_hits),
    }
    problem_json = canonical_json_bytes(problem_payload).decode("utf-8")
    rendered = template.replace(_TEMPLATE_HASH_TOKEN, template_sha256).replace(
        _PROBLEM_JSON_TOKEN,
        problem_json,
    )
    if _TEMPLATE_TOKEN.search(rendered) is not None:
        raise DirectPromptError(
            DirectPromptErrorCode.TEMPLATE_CONTRACT,
            "rendered prompt contains an unresolved template placeholder",
        )
    return RenderedDirectPrompt(
        template_id=DIRECT_AUTOFORMALIZATION_TEMPLATE_ID,
        template_version=DIRECT_AUTOFORMALIZATION_TEMPLATE_VERSION,
        template_sha256=template_sha256,
        render_sha256=sha256_hex(rendered.encode("utf-8")),
        problem_record_id=problem.problem_record_id,
        text=rendered,
    )


def _extract_single_fence(raw_output: str) -> str:
    if not raw_output.strip():
        raise DirectOutputParseError(
            DirectOutputErrorCode.EMPTY_OUTPUT,
            "provider response is empty",
        )

    lines = raw_output.splitlines()
    nonempty = [index for index, line in enumerate(lines) if line.strip()]
    assert nonempty
    first = nonempty[0]
    opener = _FENCE_LINE.fullmatch(lines[first])
    if opener is None:
        raise DirectOutputParseError(
            DirectOutputErrorCode.MISSING_FENCE,
            "response must begin with one fenced Lean declaration",
        )
    marker = opener.group(1)
    language = opener.group(2).strip().casefold()
    if language not in {"lean", "lean4"}:
        raise DirectOutputParseError(
            DirectOutputErrorCode.WRONG_FENCE_LANGUAGE,
            f"expected lean or lean4 fence, observed {language!r}",
        )

    closing: int | None = None
    for index in range(first + 1, len(lines)):
        candidate = _FENCE_LINE.fullmatch(lines[index])
        if candidate is None:
            continue
        candidate_marker = candidate.group(1)
        candidate_info = candidate.group(2).strip()
        if candidate_marker[0] == marker[0] and len(candidate_marker) >= len(marker):
            if candidate_info:
                raise DirectOutputParseError(
                    DirectOutputErrorCode.MALFORMED_FENCE,
                    "closing fence cannot carry an info string",
                )
            closing = index
            break
        raise DirectOutputParseError(
            DirectOutputErrorCode.MULTIPLE_FENCES,
            "nested or mixed fence marker found before the closing fence",
        )
    if closing is None:
        raise DirectOutputParseError(
            DirectOutputErrorCode.MALFORMED_FENCE,
            "Lean fence is not closed",
        )

    trailing = [line for line in lines[closing + 1 :] if line.strip()]
    if trailing:
        if any(_FENCE_LINE.fullmatch(line) is not None for line in trailing):
            raise DirectOutputParseError(
                DirectOutputErrorCode.MULTIPLE_FENCES,
                "response contains more than one fenced block",
            )
        raise DirectOutputParseError(
            DirectOutputErrorCode.EXTRA_TEXT,
            "response contains text outside the Lean fence",
        )
    if any(line.strip() for line in lines[:first]):
        raise DirectOutputParseError(
            DirectOutputErrorCode.EXTRA_TEXT,
            "response contains text before the Lean fence",
        )

    statement = "\n".join(lines[first + 1 : closing]).strip()
    if not statement:
        raise DirectOutputParseError(
            DirectOutputErrorCode.EMPTY_DECLARATION,
            "Lean fence is empty",
        )
    return statement


def parse_direct_autoformalization_output(raw_output: str) -> ParsedLeanDeclaration:
    """Extract exactly one proof-free theorem/lemma statement.

    This is a structural, fail-closed parser.  Lean elaboration and proposition
    validation remain a later LeanInteract step.
    """

    statement = _extract_single_fence(raw_output)
    if "--" in statement or "/-" in statement or "-/" in statement:
        raise DirectOutputParseError(
            DirectOutputErrorCode.COMMENTARY_IN_FENCE,
            "comments are forbidden in direct-autoformalization output",
        )
    if ":=" in statement or _PROOF_TOKEN.search(statement) is not None:
        raise DirectOutputParseError(
            DirectOutputErrorCode.PROOF_BEARING_OUTPUT,
            "expected a theorem statement without assignment or proof terms",
        )
    if re.search(r"(?m)^[ \t]*where\b", statement) is not None:
        raise DirectOutputParseError(
            DirectOutputErrorCode.PROOF_BEARING_OUTPUT,
            "where blocks are forbidden where a theorem statement is expected",
        )

    head = _DECLARATION_HEAD.match(statement)
    if head is None:
        raise DirectOutputParseError(
            DirectOutputErrorCode.UNSUPPORTED_DECLARATION,
            "fenced output must begin with a named theorem or lemma",
        )
    commands = _COMMAND_LINE.findall(statement)
    if len(commands) != 1:
        raise DirectOutputParseError(
            DirectOutputErrorCode.MULTIPLE_DECLARATIONS,
            f"expected exactly one declaration, observed {len(commands)} command lines",
        )
    if ":" not in statement[head.end() :]:
        raise DirectOutputParseError(
            DirectOutputErrorCode.MISSING_TYPE,
            "theorem or lemma statement has no type separator",
        )

    kind = head.group("kind")
    declaration_kind: Literal["theorem", "lemma"] = "theorem" if kind == "theorem" else "lemma"
    return ParsedLeanDeclaration(
        declaration_kind=declaration_kind,
        declaration_name=head.group("name"),
        statement=statement,
        statement_sha256=sha256_hex(statement.encode("utf-8")),
    )
