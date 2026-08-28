"""Source-level candidate cleanup, screening, and deduplication for D-2."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from leanfaith.representations.views import normalize_headless, signature_near_dup_hash

ModelFamily = Literal["goedel", "kimina", "stepfun"]
RejectionCode = Literal[
    "empty",
    "malformed_fence",
    "missing_lean_fence",
    "trailing_output",
    "unsafe_preamble",
    "declaration_count",
    "declaration_name",
    "truncated",
    "headless",
    "golden_hash",
    "golden_problem",
    "duplicate",
]

_FENCE = re.compile(r"^[ \t]*([`~]{3,})([^`~]*)[ \t]*$")
_STEPFUN_THINK_FENCE = re.compile(r"^</think>(?P<fence>[`~]{3,}[^`~]*)$")
_DECLARATION_HEAD = re.compile(
    r"^[ \t]*(?P<kind>theorem|lemma)[ \t]+(?P<name>[^\s:({\[]+)",
    re.MULTILINE | re.UNICODE,
)
_TOP_LEVEL_COMMAND = re.compile(
    r"^[ \t]*(?:theorem|lemma|def|abbrev|opaque|example|axiom|instance|structure|class|"
    r"inductive|namespace|section|end|open|import|set_option|variable|universe)\b",
    re.MULTILINE,
)
_SAFE_PREAMBLE_LINE = re.compile(
    r"(?:import[ \t]+(?:Mathlib|Aesop)(?:[ \t]+(?:Mathlib|Aesop))*|"
    r"open(?:[ \t]+scoped)?(?:[ \t]+(?:[A-Za-z0-9_'.]+|«[^»\n]+»))+|"
    r"set_option[ \t]+(?:maxHeartbeats|maxRecDepth)[ \t]+[0-9]+)"
)
_INCOMPLETE_TAIL = re.compile(
    r"(?:[:,=]|->|→|↔|∧|∨|\(|\[|\{|\+|-|\*|/|\^)\s*$"  # noqa: RUF001
)
_LET_BEFORE_ASSIGNMENT = re.compile(r"\b(?:let|have)[ \t]+[^\s:=]+(?:[ \t]*:[^;\n]+)?[ \t]*$")


class CandidateRejected(ValueError):
    """A raw completion cannot enter the D-2 candidate records."""

    def __init__(self, code: RejectionCode, detail: str) -> None:
        self.code = code
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class GoldenBlocklist:
    """The mechanically useful fields of ``golden_blocklist_v1.json``."""

    near_dup_hashes: frozenset[str]
    group_keys: frozenset[str]
    problem_names: frozenset[str]

    @classmethod
    def load(cls, path: Path) -> GoldenBlocklist:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot load golden blocklist {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError("golden blocklist must be a JSON object")
        payload = cast(dict[str, object], raw)
        versions = payload.get("version")
        hashes = payload.get("near_dup_hashes")
        groups = payload.get("group_keys")
        if (
            not isinstance(versions, list)
            or "golden_blocklist_v1" not in versions
            or not isinstance(hashes, list)
            or not isinstance(groups, list)
            or not all(isinstance(value, str) for value in hashes)
            or not all(isinstance(value, str) for value in groups)
        ):
            raise ValueError("golden blocklist fields do not match golden_blocklist_v1")
        hash_values = frozenset(cast(list[str], hashes))
        group_values = frozenset(value.casefold() for value in cast(list[str], groups))
        if any(re.fullmatch(r"[0-9a-f]{64}", value) is None for value in hash_values):
            raise ValueError("golden blocklist contains an invalid near-duplicate hash")
        return cls(
            near_dup_hashes=hash_values,
            group_keys=group_values,
            problem_names=frozenset(_bare_problem_name(value) for value in group_values),
        )

    def problem_is_blocked(self, problem_id: str) -> bool:
        folded = problem_id.strip().casefold()
        return folded in self.group_keys or _bare_problem_name(folded) in self.problem_names


@dataclass(frozen=True, slots=True)
class ProcessedCandidate:
    declaration_kind: Literal["theorem", "lemma"]
    declaration_name: str
    candidate_statement: str
    candidate_lean: str
    candidate_headless: str
    near_dup_hash: str
    #: Model-emitted, safety-validated ``open``/``set_option`` lines that the
    #: candidate was generated under (registered header excluded). Downstream
    #: typechecking must prepend these or lose legitimately-scoped candidates.
    safe_preamble: str = ""
    blocklist_screened: Literal[True] = True


def _bare_problem_name(raw: str) -> str:
    value = raw.strip().casefold().rsplit("::", 1)[-1]
    value = value.rsplit("|", 1)[-1]
    if "." in value:
        value = value.rsplit(".", 1)[-1]
    return value


def _mask_comments_and_strings(source: str) -> str:
    """Mask contents while preserving offsets and newlines."""

    chars = list(source)
    index = 0
    block_depth = 0
    in_string = False
    escaped = False
    while index < len(source):
        if block_depth:
            if source.startswith("/-", index):
                chars[index] = chars[index + 1] = " "
                block_depth += 1
                index += 2
            elif source.startswith("-/", index):
                chars[index] = chars[index + 1] = " "
                block_depth -= 1
                index += 2
            else:
                if source[index] != "\n":
                    chars[index] = " "
                index += 1
            continue
        if in_string:
            if source[index] != "\n":
                chars[index] = " "
            if escaped:
                escaped = False
            elif source[index] == "\\":
                escaped = True
            elif source[index] == '"':
                in_string = False
            index += 1
            continue
        if source.startswith("--", index):
            while index < len(source) and source[index] != "\n":
                chars[index] = " "
                index += 1
            continue
        if source.startswith("/-", index):
            chars[index] = chars[index + 1] = " "
            block_depth = 1
            index += 2
            continue
        if source[index] == '"':
            chars[index] = " "
            in_string = True
        index += 1
    if block_depth or in_string:
        raise CandidateRejected("truncated", "unterminated comment or string")
    return "".join(chars)


def _fenced_blocks(lines: list[str]) -> list[tuple[int, int, str]]:
    blocks: list[tuple[int, int, str]] = []
    index = 0
    while index < len(lines):
        opener = _FENCE.fullmatch(lines[index])
        if opener is None:
            index += 1
            continue
        marker = opener.group(1)
        closing: int | None = None
        for candidate_index in range(index + 1, len(lines)):
            candidate = _FENCE.fullmatch(lines[candidate_index])
            if candidate is None:
                continue
            candidate_marker = candidate.group(1)
            if (
                candidate_marker[0] == marker[0]
                and len(candidate_marker) >= len(marker)
                and not candidate.group(2).strip()
            ):
                closing = candidate_index
                break
        if closing is None:
            raise CandidateRejected("malformed_fence", f"unclosed fence at line {index + 1}")
        blocks.append((index, closing, opener.group(2).strip().casefold()))
        index = closing + 1
    return blocks


def _normalize_stepfun_wrapper(raw_output: str) -> str:
    lines = raw_output.splitlines()
    matches = [
        (index, match)
        for index, line in enumerate(lines)
        if (match := _STEPFUN_THINK_FENCE.fullmatch(line)) is not None
    ]
    if len(matches) > 1:
        raise CandidateRejected("malformed_fence", "multiple StepFun </think> fence boundaries")
    if matches:
        index, match = matches[0]
        lines[index] = match.group("fence")
    return "\n".join(lines)


def _select_envelope(raw_output: str, family: ModelFamily | None) -> str:
    if not raw_output.strip():
        raise CandidateRejected("empty", "completion is empty")
    if "\x00" in raw_output:
        raise CandidateRejected("truncated", "completion contains a NUL byte")
    normalized = _normalize_stepfun_wrapper(raw_output) if family == "stepfun" else raw_output
    lines = normalized.splitlines()
    has_marker = any(_FENCE.fullmatch(line) is not None for line in lines)
    if not has_marker:
        return normalized.strip()
    blocks = _fenced_blocks(lines)
    lean_blocks = [block for block in blocks if block[2] in {"lean", "lean4"}]
    if family == "kimina":
        if len(lean_blocks) != 1:
            raise CandidateRejected(
                "missing_lean_fence",
                f"Kimina requires exactly one Lean fence, observed {len(lean_blocks)}",
            )
        selected = lean_blocks[0]
    else:
        if not blocks or blocks[-1][2] not in {"lean", "lean4"}:
            raise CandidateRejected("missing_lean_fence", "terminal block is not Lean")
        selected = blocks[-1]
    opening, closing, _ = selected
    if any(line.strip() for line in lines[closing + 1 :]):
        raise CandidateRejected("trailing_output", "Lean fence is not terminal")
    code = "\n".join(lines[opening + 1 : closing]).strip()
    if not code:
        raise CandidateRejected("empty", "selected Lean fence is empty")
    return code


def _validate_safe_preamble(prefix: str) -> None:
    masked = _mask_comments_and_strings(prefix)
    for line in masked.splitlines():
        stripped = line.strip()
        if stripped and _SAFE_PREAMBLE_LINE.fullmatch(stripped) is None:
            raise CandidateRejected(
                "unsafe_preamble", f"unsupported text before declaration: {stripped}"
            )


def _declaration_body(code: str, registered_header: str) -> tuple[str, str]:
    header = registered_header.rstrip()
    if header and (code == header or code.startswith(header + "\n")):
        code = code[len(header) :].lstrip("\n")
    masked = _mask_comments_and_strings(code)
    heads = tuple(_DECLARATION_HEAD.finditer(masked))
    if len(heads) != 1:
        raise CandidateRejected(
            "declaration_count",
            f"expected one theorem or lemma, observed {len(heads)}",
        )
    head = heads[0]
    preamble_text = code[: head.start()]
    _validate_safe_preamble(preamble_text)
    preamble = "\n".join(line.strip() for line in preamble_text.splitlines() if line.strip())
    body = code[head.start() :].strip()
    body_masked = _mask_comments_and_strings(body)
    commands = tuple(_TOP_LEVEL_COMMAND.finditer(body_masked))
    if len(commands) != 1 or commands[0].start() != 0:
        raise CandidateRejected("declaration_count", "candidate contains another top-level command")
    return preamble, body


def _top_level_positions(source: str) -> tuple[int, list[int]]:
    masked = _mask_comments_and_strings(source)
    stack: list[str] = []
    pairs = {")": "(", "]": "[", "}": "{"}
    result_colon: int | None = None
    assignments: list[int] = []
    index = 0
    while index < len(masked):
        char = masked[index]
        if char in "([{":
            stack.append(char)
        elif char in ")]}":
            if not stack or stack[-1] != pairs[char]:
                raise CandidateRejected("truncated", f"unbalanced delimiter {char!r}")
            stack.pop()
        elif not stack and char == ":":
            if index + 1 < len(masked) and masked[index + 1] == "=":
                if result_colon is not None:
                    assignments.append(index)
                index += 1
            elif result_colon is None:
                result_colon = index
        index += 1
    if stack:
        raise CandidateRejected("truncated", "unclosed Lean delimiter")
    if result_colon is None:
        raise CandidateRejected("truncated", "declaration has no top-level result colon")
    return result_colon, assignments


def _strip_proof(source: str) -> str:
    result_colon, assignments = _top_level_positions(source)
    masked = _mask_comments_and_strings(source)
    usable = [
        position
        for position in assignments
        if _LET_BEFORE_ASSIGNMENT.search(masked[result_colon + 1 : position]) is None
    ]
    proof_start: int | None = None
    for position in usable:
        tail = masked[position + 2 :].lstrip()
        if re.match(r"(?:by|sorry|admit)\b", tail):
            proof_start = position
            break
    if proof_start is None and usable:
        proof_start = usable[-1]
    statement = source[:proof_start].rstrip() if proof_start is not None else source.strip()
    result_colon, _ = _top_level_positions(statement)
    type_text = _mask_comments_and_strings(statement)[result_colon + 1 :].strip()
    if not type_text or _INCOMPLETE_TAIL.search(type_text):
        raise CandidateRejected("truncated", "declaration result type is empty or incomplete")
    return statement


def postprocess_candidate(
    raw_output: str,
    *,
    problem_id: str,
    registered_header: str,
    blocklist: GoldenBlocklist,
    family: ModelFamily | None = None,
    expected_declaration_name: str | None = None,
    seen_hashes: set[str] | None = None,
) -> ProcessedCandidate:
    """Extract one proof-free source declaration and apply D-2 screens."""

    code = _select_envelope(raw_output, family)
    safe_preamble, body = _declaration_body(code, registered_header)
    head = _DECLARATION_HEAD.match(_mask_comments_and_strings(body))
    assert head is not None
    declaration_name = head.group("name")
    if expected_declaration_name is not None and declaration_name != expected_declaration_name:
        raise CandidateRejected(
            "declaration_name",
            f"expected {expected_declaration_name!r}, observed {declaration_name!r}",
        )
    statement = _strip_proof(body)
    candidate_lean = statement + " := by sorry"
    headless = normalize_headless(candidate_lean)
    if headless is None:
        raise CandidateRejected("headless", "normalize_headless returned no statement")
    near_dup_hash = signature_near_dup_hash(headless)
    if blocklist.problem_is_blocked(problem_id):
        raise CandidateRejected("golden_problem", f"problem {problem_id!r} is in group_keys")
    if near_dup_hash in blocklist.near_dup_hashes:
        raise CandidateRejected("golden_hash", "candidate headless hash is in the golden blocklist")
    if seen_hashes is not None and near_dup_hash in seen_hashes:
        raise CandidateRejected("duplicate", "candidate duplicates an earlier batch output")
    if seen_hashes is not None:
        seen_hashes.add(near_dup_hash)
    return ProcessedCandidate(
        declaration_kind=cast(Literal["theorem", "lemma"], head.group("kind")),
        declaration_name=declaration_name,
        candidate_statement=statement,
        candidate_lean=candidate_lean,
        candidate_headless=headless,
        near_dup_hash=near_dup_hash,
        safe_preamble=safe_preamble,
    )


__all__ = [
    "CandidateRejected",
    "GoldenBlocklist",
    "ModelFamily",
    "ProcessedCandidate",
    "RejectionCode",
    "postprocess_candidate",
]
