"""Lean-free, resumable inventory for pinned CPT2 proof-bearing compiler rows.

The inventory is deliberately a cheap phase.  It verifies immutable local
release artifacts, keeps only ``label=True`` rows, reconstructs the exact Lean
source, derives conservative textual signatures and context fingerprints, and
deduplicates with a transactional SQLite index.  It never imports or invokes
Lean/LeanInteract.

Completed input shards are committed atomically in SQLite.  Final inventory
JSONL shards are deterministic hash partitions with manifest-last receipts, so
an interrupted run can safely resume without changing already completed data.
"""

from __future__ import annotations

import argparse
import hashlib
import heapq
import json
import os
import re
import sqlite3
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]
import yaml

from leanfaith.config import hashing as hashing_module
from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.config.paths import find_repo_root
from leanfaith.cpt2 import splitters as cpt2_splitters
from leanfaith.cpt2.splitters import mask_lean_source
from leanfaith.representations import views as representation_views
from leanfaith.representations.views import signature_near_dup_hash
from leanfaith.sft1.sprint.store import Journal, write_atomic

INVENTORY_SCHEMA_VERSION = "sft1_cpt2_compiler_inventory_v1"
NORMALIZATION_VERSION = "lean_name_free_quote_aware_layout_v1"
FEATURE_VERSION = "sft1_compiler_signature_features_v1"
LENGTH_STRATA_VERSION = "sft1_compiler_fixed_character_bins_v1"
RUN_SPEC_VERSION = "sft1_cpt2_inventory_run_v1"
AUDIT_SAMPLE_VERSION = "sft1_compiler_stratified_hash_sample_v2"
SUPPORTED_FEATURES = frozenset(
    {
        "equality",
        "disequality",
        "strict_order",
        "non_strict_order",
        "bounded_quantifier",
        "existential",
        "implication",
        "membership",
        "universe",
        "typeclass",
        "numeral",
    }
)

_RELEASE_SCHEMA = pa.schema(
    [
        pa.field("theorem", pa.large_string(), nullable=False),
        pa.field("body", pa.large_string(), nullable=False),
        pa.field("label", pa.bool_(), nullable=False),
    ]
)
_DECLARATION_TOKEN = re.compile(r"(?<![\w'])\b(?P<kind>theorem|lemma)\b(?![\w'])")
_CONTEXT_DECLARATION_TOKEN = re.compile(
    r"(?<![\w'])\b(?:theorem|lemma|def|abbrev|opaque|axiom|example|instance|"
    r"class|structure|inductive|coinductive)\b(?![\w'])"
)
_HASH = re.compile(r"[0-9a-f]{64}")
_REVISION = re.compile(r"[0-9a-f]{40}")
_IMPORT = re.compile(r"^(?:(?:public|meta)\s+)*import\s+(?P<modules>\S+(?:\s+\S+)*)$")
_NAMESPACE = re.compile(r"^namespace\s+(?P<name>[A-Za-z_][A-Za-z0-9_'.]*)$")
_SECTION = re.compile(r"^(?:noncomputable\s+)?section(?:\s+(?P<name>\S+))?$")
_END = re.compile(r"^end(?:\s+(?P<name>\S+))?$")
_EXACT_EQ = re.compile(r"(?<![:<>=!])=(?!=|>)")
_EXACT_LT = re.compile(r"(?<![<:=])<(?![=>-])")
_TYPECLASS_BINDER = re.compile(r"\[[^\[\]]{1,2000}\]")
_NUMERAL = re.compile(r"(?<![\w'])\d+(?![\w'])")
_FORALL = re.compile(r"(?:∀|\bforall\b)")
_EXISTS = re.compile(r"(?:∃|\bExists\b)")
_IMPLICATION = re.compile(r"(?:→|(?<!-)\->)")
_UNIVERSE = re.compile(r"(?:\buniverse\b|\bType(?:\s+[A-Za-z0-9_]+)?\b|\bSort\b)")


class CompilerInventoryError(RuntimeError):
    """The pinned input or durable inventory state is inconsistent."""


class CompilerRowRejected(ValueError):
    """A valid-labeled row cannot be conservatively inventoried."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Cpt2ReleasePin:
    repo_id: str
    final_revision: str
    data_commit: str
    manifest_sha256: str
    publication_receipt_sha256: str
    release_tree_sha256: str
    cpt2_run_id: str
    source_repo_id: str
    source_revision: str
    source_parquet_path: str
    source_parquet_sha256: str
    expected_release_rows: int
    expected_valid_rows: int
    expected_valid_exact_prefixes: int | None

    def __post_init__(self) -> None:
        for label, value in (
            ("final_revision", self.final_revision),
            ("data_commit", self.data_commit),
            ("source_revision", self.source_revision),
        ):
            if _REVISION.fullmatch(value) is None:
                raise ValueError(f"{label} must be a 40-character lowercase Git revision")
        for label, value in (
            ("manifest_sha256", self.manifest_sha256),
            ("publication_receipt_sha256", self.publication_receipt_sha256),
            ("release_tree_sha256", self.release_tree_sha256),
            ("cpt2_run_id", self.cpt2_run_id),
            ("source_parquet_sha256", self.source_parquet_sha256),
        ):
            if _HASH.fullmatch(value) is None:
                raise ValueError(f"{label} must be a 64-character lowercase SHA-256")
        if self.expected_release_rows <= 0 or self.expected_valid_rows <= 0:
            raise ValueError("pinned release counts must be positive")

    def to_dict(self) -> dict[str, object]:
        return {
            "repo_id": self.repo_id,
            "final_revision": self.final_revision,
            "data_commit": self.data_commit,
            "manifest_sha256": self.manifest_sha256,
            "publication_receipt_sha256": self.publication_receipt_sha256,
            "release_tree_sha256": self.release_tree_sha256,
            "cpt2_run_id": self.cpt2_run_id,
            "source_repo_id": self.source_repo_id,
            "source_revision": self.source_revision,
            "source_parquet_path": self.source_parquet_path,
            "source_parquet_sha256": self.source_parquet_sha256,
            "expected_release_rows": self.expected_release_rows,
            "expected_valid_rows": self.expected_valid_rows,
            "expected_valid_exact_prefixes": self.expected_valid_exact_prefixes,
        }


@dataclass(frozen=True, slots=True)
class CompilerProjectContext:
    project_id: str
    project_revision: str
    lean_version: str
    lean_interact_version: str
    repl_revision: str
    checker_version: str

    def to_dict(self) -> dict[str, str]:
        return {
            "project_id": self.project_id,
            "project_revision": self.project_revision,
            "lean_version": self.lean_version,
            "lean_interact_version": self.lean_interact_version,
            "repl_revision": self.repl_revision,
            "checker_version": self.checker_version,
        }


@dataclass(frozen=True, slots=True)
class AuditSampleSettings:
    size: int
    salt: str
    features: tuple[str, ...]
    length_dimensions: tuple[str, ...]
    include_context_frequency_strata: bool
    include_namespace_status_strata: bool
    include_context_complexity_strata: bool

    def __post_init__(self) -> None:
        if self.size <= 0 or not self.salt:
            raise ValueError("audit sample size and salt must be positive/non-empty")
        if not self.features or len(set(self.features)) != len(self.features):
            raise ValueError("audit sample features must be non-empty and unique")
        if set(self.features) - SUPPORTED_FEATURES:
            raise ValueError("audit sample has unsupported signature features")
        allowed_dimensions = {"signature", "theorem", "body", "full_source"}
        if not self.length_dimensions or set(self.length_dimensions) - allowed_dimensions:
            raise ValueError("audit sample has unsupported length dimensions")

    def to_dict(self) -> dict[str, object]:
        return {
            "size": self.size,
            "salt": self.salt,
            "features": list(self.features),
            "length_dimensions": list(self.length_dimensions),
            "include_context_frequency_strata": self.include_context_frequency_strata,
            "include_namespace_status_strata": self.include_namespace_status_strata,
            "include_context_complexity_strata": self.include_context_complexity_strata,
            "selection_version": AUDIT_SAMPLE_VERSION,
        }


@dataclass(frozen=True, slots=True)
class InventorySettings:
    release_root: Path
    manifest_path: Path
    publication_receipt_path: Path
    output_root: Path
    gold_blocklist_path: Path
    gold_blocklist_sha256: str
    pin: Cpt2ReleasePin
    project: CompilerProjectContext
    audit_sample: AuditSampleSettings | None = None
    config_sha256: str | None = None
    splits: tuple[str, ...] = ("train", "validation")
    output_shards: int = 256
    batch_rows: int = 8192
    verify_input_file_hashes: bool = True

    def __post_init__(self) -> None:
        if not self.splits or len(set(self.splits)) != len(self.splits):
            raise ValueError("inventory splits must be non-empty and unique")
        if set(self.splits) - {"train", "validation"}:
            raise ValueError("inventory splits may only contain train and validation")
        if self.output_shards <= 0 or self.batch_rows <= 0:
            raise ValueError("inventory shard and batch sizes must be positive")
        if not self.verify_input_file_hashes:
            raise ValueError("Wave 5 inventory requires input file hash verification")
        if _HASH.fullmatch(self.gold_blocklist_sha256) is None:
            raise ValueError("gold_blocklist_sha256 must be a lowercase SHA-256")
        if self.config_sha256 is not None and _HASH.fullmatch(self.config_sha256) is None:
            raise ValueError("config_sha256 must be a lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class InputShard:
    part: int
    total_parts: int
    split: str
    file: str
    path: Path
    sha256: str
    rows: int
    valid_rows: int

    @property
    def shard_id(self) -> str:
        return hash_canonical(
            {
                "kind": "cpt2_release_input_shard",
                "split": self.split,
                "part": self.part,
                "total_parts": self.total_parts,
                "file": self.file,
                "sha256": self.sha256,
                "rows": self.rows,
            }
        )

    def identity(self) -> dict[str, object]:
        return {
            "shard_id": self.shard_id,
            "part": self.part,
            "total_parts": self.total_parts,
            "split": self.split,
            "file": self.file,
            "sha256": self.sha256,
            "rows": self.rows,
            "valid_rows": self.valid_rows,
        }


@dataclass(frozen=True, slots=True)
class SignatureInfo:
    declaration_kind: str
    declaration_name: str
    declaration_name_is_rooted: bool
    declaration_offset: int
    assignment_offset: int
    context_prefix: str
    exact_signature: str
    normalized_signature: str


@dataclass(frozen=True, slots=True)
class CompilerRecordDraft:
    theorem: str
    body: str
    exact_signature: str
    normalized_signature: str
    record: dict[str, object]


@dataclass(frozen=True, slots=True)
class InventoryResult:
    manifest_path: Path
    audit_sample_path: Path | None
    run_id: str
    output_rows: int
    written_input_shards: int
    resumed_input_shards: int
    written_output_shards: int
    resumed_output_shards: int


ContaminationHook = Callable[[CompilerRecordDraft], str | None]


def reconstruct_source(theorem: str, body: str) -> str:
    """Reconstruct the exact CPT2 source around the excluded literal ``by`` token."""

    return theorem + "by" + body


def _is_identifier_char(char: str) -> bool:
    return char == "_" or char == "'" or char.isalnum()


def _char_literal_finish(source: str, start: int) -> int | None:
    if start > 0 and _is_identifier_char(source[start - 1]):
        return None
    index = start + 1
    if index >= len(source) or source[index] in "\r\n'":
        return None
    escaped = False
    while index < len(source) and index - start <= 32:
        char = source[index]
        if char in "\r\n" and not escaped:
            return None
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "'":
            return index + 1
        index += 1
    return None


def normalize_lean_layout(text: str) -> str:
    """Remove comments and collapse layout without changing quoted Lean tokens.

    String literals, character literals, and guillemet identifiers are copied
    byte-for-byte.  No alpha-renaming, notation rewriting, or semantic claim is
    made by this normalizer.
    """

    output: list[str] = []
    pending_space = False
    index = 0
    block_depth = 0
    in_string = False
    in_guillemet = False
    escaped = False

    def emit_space() -> None:
        nonlocal pending_space
        if pending_space and output:
            output.append(" ")
        pending_space = False

    while index < len(text):
        char = text[index]
        if block_depth:
            if text.startswith("/-", index):
                block_depth += 1
                index += 2
            elif text.startswith("-/", index):
                block_depth -= 1
                index += 2
            else:
                index += 1
            pending_space = True
            continue
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if in_guillemet:
            output.append(char)
            if char == "»":
                in_guillemet = False
            index += 1
            continue
        if text.startswith("--", index):
            newline = text.find("\n", index + 2)
            index = len(text) if newline < 0 else newline + 1
            pending_space = True
            continue
        if text.startswith("/-", index):
            block_depth = 1
            index += 2
            pending_space = True
            continue
        if char.isspace():
            pending_space = True
            index += 1
            continue
        literal_finish = _char_literal_finish(text, index) if char == "'" else None
        if literal_finish is not None:
            emit_space()
            output.append(text[index:literal_finish])
            index = literal_finish
            continue
        emit_space()
        output.append(char)
        if char == '"':
            in_string = True
        elif char == "«":
            in_guillemet = True
        index += 1
    if block_depth:
        raise CompilerRowRejected("unterminated_block_comment")
    if in_string:
        raise CompilerRowRejected("unterminated_string_literal")
    if in_guillemet:
        raise CompilerRowRejected("unterminated_guillemet_identifier")
    return "".join(output).strip()


def _lean_name_end(source: str, start: int) -> int | None:
    index = start
    while index < len(source) and source[index].isspace():
        index += 1
    name_start = index
    in_guillemet = False
    while index < len(source):
        char = source[index]
        if in_guillemet:
            if char == "»":
                in_guillemet = False
            index += 1
            continue
        if char == "«":
            in_guillemet = True
            index += 1
            continue
        if char.isspace() or char in ":([{⦃":
            break
        index += 1
    if in_guillemet or index == name_start:
        return None
    return index


def _top_level_assignment(masked: str, start: int) -> int | None:
    stack: list[str] = []
    pairs = {")": "(", "]": "[", "}": "{", "⦄": "⦃", "⟩": "⟨"}
    assignment: int | None = None
    for index in range(start, len(masked) - 1):
        char = masked[index]
        if char in "([{⦃⟨":
            stack.append(char)
        elif char in ")]}⦄⟩":
            if not stack or stack[-1] != pairs[char]:
                return None
            stack.pop()
        elif not stack and masked.startswith(":=", index):
            # ``let x := value`` is legal in a theorem target.  The CPT2
            # theorem field ends at the declaration assignment, so the final
            # top-level assignment is the boundary we need.
            assignment = index
    return assignment


def extract_theorem_signature(theorem: str) -> SignatureInfo:
    """Extract the final name-free declaration signature from a CPT2 prefix."""

    masked = mask_lean_source(theorem)
    for match in reversed(tuple(_DECLARATION_TOKEN.finditer(masked))):
        name_end = _lean_name_end(theorem, match.end())
        if name_end is None:
            continue
        assignment = _top_level_assignment(masked, name_end)
        if assignment is None or masked[assignment + 2 :].strip():
            continue
        raw_name = theorem[match.end() : name_end].strip()
        exact_signature = theorem[name_end:assignment]
        normalized = normalize_lean_layout(exact_signature)
        if not normalized or ":" not in mask_lean_source(normalized):
            continue
        return SignatureInfo(
            declaration_kind=match.group("kind"),
            declaration_name=raw_name.removeprefix("_root_."),
            declaration_name_is_rooted=raw_name.startswith("_root_."),
            declaration_offset=match.start(),
            assignment_offset=assignment,
            context_prefix=theorem[: match.start()],
            exact_signature=exact_signature,
            normalized_signature=normalized,
        )
    raise CompilerRowRejected("final_theorem_signature_not_found")


def _context_commands(
    context_prefix: str,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    masked = mask_lean_source(context_prefix)
    imports: list[str] = []
    options: list[str] = []
    opens: list[str] = []
    includes: list[str] = []
    for raw_line in masked.splitlines():
        line = " ".join(raw_line.split())
        if not line:
            continue
        import_match = _IMPORT.fullmatch(line)
        if import_match is not None:
            imports.extend(import_match.group("modules").split())
        elif line.startswith("set_option "):
            options.append(line)
        elif line.startswith("open "):
            opens.append(line)
        elif line.startswith("include "):
            includes.append(line)
    return tuple(imports), tuple(options), tuple(opens), tuple(includes)


def _namespace_context(context_prefix: str) -> tuple[tuple[str, ...], str]:
    """Return the active simple namespace stack and a conservative status."""

    stack: list[tuple[str, str | None]] = []
    exact = True
    for raw_line in mask_lean_source(context_prefix).splitlines():
        line = " ".join(raw_line.split())
        if not line:
            continue
        namespace = _NAMESPACE.fullmatch(line)
        section = _SECTION.fullmatch(line)
        end = _END.fullmatch(line)
        if namespace is not None:
            stack.append(("namespace", namespace.group("name")))
        elif line == "namespace" or line.startswith("namespace "):
            exact = False
        elif section is not None:
            stack.append(("section", section.group("name")))
        elif line == "section" or line.startswith("section "):
            exact = False
        elif end is not None:
            if not stack:
                exact = False
                continue
            expected = end.group("name")
            if expected is None:
                stack.pop()
                continue
            matching = next(
                (index for index in range(len(stack) - 1, -1, -1) if stack[index][1] == expected),
                None,
            )
            if matching is None:
                exact = False
            else:
                del stack[matching:]
        elif line == "end" or line.startswith("end "):
            exact = False
    namespaces = tuple(name for kind, name in stack if kind == "namespace" and name is not None)
    return namespaces, "simple_namespace_stack_v1" if exact else "requires_lean_verification"


def _context_complexity(context_prefix: str) -> tuple[int, str]:
    """Classify prior declarations that must replay before the selected theorem."""

    declaration_count = len(
        tuple(_CONTEXT_DECLARATION_TOKEN.finditer(mask_lean_source(context_prefix)))
    )
    if declaration_count == 0:
        status = "no_preceding_declarations"
    elif declaration_count == 1:
        status = "one_preceding_declaration"
    else:
        status = "multiple_preceding_declarations"
    return declaration_count, status


def signature_features(signature: str) -> tuple[str, ...]:
    """Conservative multi-label features used for Lean-free stratification."""

    visible = mask_lean_source(signature)
    features: list[str] = []
    if _EXACT_EQ.search(visible):
        features.append("equality")
    if "≠" in visible or re.search(r"\bNot\b[^\n]{0,1000}=", visible):
        features.append("disequality")
    if _EXACT_LT.search(visible):
        features.append("strict_order")
    if "≤" in visible or "<=" in visible:
        features.append("non_strict_order")
    if (_FORALL.search(visible) or _EXISTS.search(visible)) and (
        "Finset.range" in visible
        or "∈" in visible
        or _EXACT_LT.search(visible)
        or "≤" in visible
        or "<=" in visible
    ):
        features.append("bounded_quantifier")
    if _EXISTS.search(visible):
        features.append("existential")
    if _IMPLICATION.search(visible):
        features.append("implication")
    if "∈" in visible or "Membership.mem" in visible:
        features.append("membership")
    if _UNIVERSE.search(visible):
        features.append("universe")
    if _TYPECLASS_BINDER.search(visible):
        features.append("typeclass")
    if _NUMERAL.search(visible):
        features.append("numeral")
    return tuple(features)


def length_stratum(length: int) -> str:
    if length <= 128:
        return "000_128"
    if length <= 256:
        return "129_256"
    if length <= 512:
        return "257_512"
    if length <= 1024:
        return "513_1024"
    if length <= 4096:
        return "1025_4096"
    return "4097_plus"


def build_compiler_record(
    *,
    theorem: str,
    body: str,
    row_index: int,
    shard: InputShard,
    pin: Cpt2ReleasePin,
    project: CompilerProjectContext,
) -> CompilerRecordDraft:
    """Build one content-addressed record without invoking Lean."""

    if row_index < 0:
        raise ValueError("row_index must be non-negative")
    signature = extract_theorem_signature(theorem)
    full_source = reconstruct_source(theorem, body)
    theorem_sha256 = sha256_hex(theorem.encode("utf-8"))
    body_sha256 = sha256_hex(body.encode("utf-8"))
    full_source_sha256 = sha256_hex(full_source.encode("utf-8"))
    exact_signature_sha256 = sha256_hex(signature.exact_signature.encode("utf-8"))
    normalized_signature_sha256 = sha256_hex(signature.normalized_signature.encode("utf-8"))
    context_sha256 = sha256_hex(signature.context_prefix.encode("utf-8"))
    imports, option_commands, open_commands, include_commands = _context_commands(
        signature.context_prefix
    )
    namespace_stack, namespace_status = _namespace_context(signature.context_prefix)
    preceding_declarations, context_complexity = _context_complexity(signature.context_prefix)
    qualified_name = signature.declaration_name
    if not signature.declaration_name_is_rooted and namespace_status == "simple_namespace_stack_v1":
        qualified_name = ".".join((*namespace_stack, signature.declaration_name))
    project_payload = project.to_dict()
    project_fingerprint = hash_canonical(project_payload)
    context_fingerprint = hash_canonical(
        {
            "kind": "compiler_context",
            "version": 1,
            "context_sha256": context_sha256,
            "imports": list(imports),
            "option_commands": list(option_commands),
            "open_commands": list(open_commands),
            "include_commands": list(include_commands),
            "namespace_stack": list(namespace_stack),
            "namespace_status": namespace_status,
            "preceding_declarations": preceding_declarations,
            "context_complexity": context_complexity,
            "project_fingerprint": project_fingerprint,
            "normalization_version": NORMALIZATION_VERSION,
        }
    )
    normalized_group_id = hash_canonical(
        {
            "kind": "contextual_normalized_compiler_signature",
            "version": 1,
            "normalized_signature_sha256": normalized_signature_sha256,
            "context_fingerprint": context_fingerprint,
        }
    )
    source_row_id = hash_canonical(
        {
            "kind": "cpt2_release_row",
            "repo_id": pin.repo_id,
            "revision": pin.final_revision,
            "data_commit": pin.data_commit,
            "split": shard.split,
            "shard_file": shard.file,
            "shard_sha256": shard.sha256,
            "row_index": row_index,
            "theorem_sha256": theorem_sha256,
            "body_sha256": body_sha256,
        }
    )
    root_id = hash_canonical(
        {
            "kind": "sft1_compiler_root",
            "schema_version": INVENTORY_SCHEMA_VERSION,
            "source_row_id": source_row_id,
            "full_source_sha256": full_source_sha256,
            "normalized_signature_sha256": normalized_signature_sha256,
        }
    )
    lengths = {
        "theorem_characters": len(theorem),
        "body_characters": len(body),
        "full_source_characters": len(full_source),
        "signature_characters": len(signature.normalized_signature),
        "theorem_stratum": length_stratum(len(theorem)),
        "body_stratum": length_stratum(len(body)),
        "full_source_stratum": length_stratum(len(full_source)),
        "signature_stratum": length_stratum(len(signature.normalized_signature)),
    }
    record: dict[str, object] = {
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "root_id": root_id,
        "source_row_id": source_row_id,
        "normalized_group_id": normalized_group_id,
        "source": {
            "release_id": hash_canonical(pin.to_dict()),
            "split": shard.split,
            "part": shard.part,
            "shard_file": shard.file,
            "shard_sha256": shard.sha256,
            "row_index": row_index,
        },
        "hashes": {
            "theorem_sha256": theorem_sha256,
            "body_sha256": body_sha256,
            "full_source_sha256": full_source_sha256,
            "exact_signature_sha256": exact_signature_sha256,
            "normalized_signature_sha256": normalized_signature_sha256,
        },
        "declaration": {
            "kind": signature.declaration_kind,
            "name": signature.declaration_name,
            "name_is_rooted": signature.declaration_name_is_rooted,
            "qualified_name_candidate": (
                qualified_name if namespace_status == "simple_namespace_stack_v1" else None
            ),
            "qualified_name_status": namespace_status,
            "offset": signature.declaration_offset,
            "assignment_offset": signature.assignment_offset,
        },
        "context": {
            "context_sha256": context_sha256,
            "context_fingerprint": context_fingerprint,
            "project_fingerprint": project_fingerprint,
            "imports": list(imports),
            "option_commands": list(option_commands),
            "open_commands": list(open_commands),
            "include_commands": list(include_commands),
            "namespace_stack": list(namespace_stack),
            "namespace_status": namespace_status,
            "preceding_declarations": preceding_declarations,
            "context_complexity": context_complexity,
        },
        "features": list(signature_features(signature.normalized_signature)),
        "lengths": lengths,
    }
    return CompilerRecordDraft(
        theorem=theorem,
        body=body,
        exact_signature=signature.exact_signature,
        normalized_signature=signature.normalized_signature,
        record=record,
    )


@dataclass(frozen=True, slots=True)
class GoldenBlocklistHook:
    sha256: str
    hashes: frozenset[str]

    @classmethod
    def load(cls, path: Path, expected_sha256: str) -> GoldenBlocklistHook:
        observed = hash_file(path)
        if observed != expected_sha256:
            raise CompilerInventoryError(
                f"gold blocklist hash mismatch: expected {expected_sha256}, got {observed}"
            )
        payload = _read_json(path)
        values = payload.get("near_dup_hashes")
        versions = payload.get("version")
        group_keys = payload.get("group_keys")
        if not isinstance(values, list) or not all(
            isinstance(value, str) and _HASH.fullmatch(value) is not None for value in values
        ):
            raise CompilerInventoryError("gold blocklist near_dup_hashes are malformed")
        if not isinstance(versions, list) or "golden_blocklist_v1" not in versions:
            raise CompilerInventoryError("gold blocklist version is not golden_blocklist_v1")
        if not isinstance(group_keys, list) or not all(
            isinstance(value, str) for value in group_keys
        ):
            raise CompilerInventoryError("gold blocklist group_keys are malformed")
        return cls(sha256=observed, hashes=frozenset(cast(list[str], values)))

    def __call__(self, draft: CompilerRecordDraft) -> str | None:
        hashes = cast(dict[str, object], draft.record["hashes"])
        if hashes["theorem_sha256"] in self.hashes:
            return "gold_exact_theorem_hash"
        if hashes["normalized_signature_sha256"] in self.hashes:
            return "gold_normalized_signature_hash"
        if signature_near_dup_hash(draft.normalized_signature) in self.hashes:
            return "gold_signature_near_duplicate"
        return None


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CompilerInventoryError(f"cannot read JSON object {path}: {error}") from error
    if not isinstance(value, dict):
        raise CompilerInventoryError(f"expected JSON object at {path}")
    return cast(dict[str, Any], value)


def _mapping(value: object, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CompilerInventoryError(f"{context} must be a mapping")
    return cast(dict[str, Any], value)


def _require_equal(actual: object, expected: object, context: str) -> None:
    if actual != expected:
        raise CompilerInventoryError(f"{context} mismatch: expected {expected!r}, got {actual!r}")


def _resolve_path(root: Path, value: object, context: str) -> Path:
    if not isinstance(value, str) or not value:
        raise CompilerInventoryError(f"{context} must be a non-empty path")
    path = Path(value)
    return path if path.is_absolute() else root / path


def load_inventory_config(path: Path) -> InventorySettings:
    """Load the additive Wave 5 inventory config without any network access."""

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise CompilerInventoryError(f"cannot load inventory config {path}: {error}") from error
    document = _mapping(raw, "inventory config")
    _require_equal(
        document.get("schema_version"),
        "sft1_wave5_compiler_inventory_config_v1",
        "inventory config schema_version",
    )
    _require_equal(document.get("sprint_id"), "sft1_wave5_compiler_core_v1", "inventory sprint_id")
    repo_root = find_repo_root(path)
    source = _mapping(document.get("source"), "source")
    project = _mapping(document.get("project"), "project")
    inventory = _mapping(document.get("inventory"), "inventory")
    contamination = _mapping(document.get("contamination"), "contamination")
    audit = _mapping(document.get("audit_sample"), "audit_sample")
    execution = _mapping(document.get("execution"), "execution")
    release = _mapping(document.get("release"), "release")
    pin = Cpt2ReleasePin(
        repo_id=str(source["repo_id"]),
        final_revision=str(source["final_revision"]),
        data_commit=str(source["data_commit"]),
        manifest_sha256=str(source["manifest_sha256"]),
        publication_receipt_sha256=str(source["publication_receipt_sha256"]),
        release_tree_sha256=str(source["release_tree_sha256"]),
        cpt2_run_id=str(source["cpt2_run_id"]),
        source_repo_id=str(source["upstream_repo_id"]),
        source_revision=str(source["upstream_revision"]),
        source_parquet_path=str(source["upstream_parquet_path"]),
        source_parquet_sha256=str(source["upstream_parquet_sha256"]),
        expected_release_rows=int(source["expected_release_rows"]),
        expected_valid_rows=int(source["expected_valid_rows"]),
        expected_valid_exact_prefixes=(
            int(source["expected_valid_exact_prefixes"])
            if source.get("expected_valid_exact_prefixes") is not None
            else None
        ),
    )
    project_context = CompilerProjectContext(
        project_id=str(project["project_id"]),
        project_revision=str(project["project_revision"]),
        lean_version=str(project["lean_version"]),
        lean_interact_version=str(project["lean_interact_version"]),
        repl_revision=str(project["repl_revision"]),
        checker_version=str(project["checker_version"]),
    )
    splits_value = inventory.get("splits")
    if not isinstance(splits_value, list) or not all(
        isinstance(item, str) for item in splits_value
    ):
        raise CompilerInventoryError("inventory.splits must be a string list")
    _require_equal(inventory.get("required_label"), True, "inventory.required_label")
    _require_equal(
        inventory.get("normalization_version"),
        NORMALIZATION_VERSION,
        "inventory.normalization_version",
    )
    _require_equal(inventory.get("feature_version"), FEATURE_VERSION, "inventory.feature_version")
    _require_equal(
        inventory.get("length_strata_version"),
        LENGTH_STRATA_VERSION,
        "inventory.length_strata_version",
    )
    _require_equal(
        inventory.get("reconstruction"),
        "theorem_plus_literal_by_plus_body",
        "inventory.reconstruction",
    )
    _require_equal(
        inventory.get("dedup_order"),
        ["exact_theorem_prefix", "context_qualified_normalized_name_free_signature"],
        "inventory.dedup_order",
    )
    _require_equal(
        inventory.get("representative_order"),
        ["body_characters", "body_sha256", "source_row_id"],
        "inventory.representative_order",
    )
    _require_equal(inventory.get("lean_calls"), 0, "inventory.lean_calls")
    _require_equal(contamination.get("action"), "exclude_before_dedup", "contamination.action")
    _require_equal(
        contamination.get("additional_hook"),
        "supported_by_compiler_inventory_api_with_content_digest",
        "contamination.additional_hook",
    )
    audit_features = audit.get("multi_label_features")
    audit_lengths = audit.get("length_dimensions")
    if not isinstance(audit_features, list) or not all(
        isinstance(item, str) for item in audit_features
    ):
        raise CompilerInventoryError("audit_sample.multi_label_features must be a string list")
    if not isinstance(audit_lengths, list) or not all(
        isinstance(item, str) for item in audit_lengths
    ):
        raise CompilerInventoryError("audit_sample.length_dimensions must be a string list")
    include_context = audit.get("include_common_and_rare_context_fingerprints")
    if type(include_context) is not bool:
        raise CompilerInventoryError("audit context-strata flag must be boolean")
    include_namespace_status = audit.get("include_namespace_status_strata")
    if type(include_namespace_status) is not bool:
        raise CompilerInventoryError("audit namespace-status flag must be boolean")
    include_context_complexity = audit.get("include_context_complexity_strata")
    if type(include_context_complexity) is not bool:
        raise CompilerInventoryError("audit context-complexity flag must be boolean")
    audit_settings = AuditSampleSettings(
        size=int(audit["size"]),
        salt=str(audit["salt"]),
        features=tuple(cast(list[str], audit_features)),
        length_dimensions=tuple(cast(list[str], audit_lengths)),
        include_context_frequency_strata=include_context,
        include_namespace_status_strata=include_namespace_status,
        include_context_complexity_strata=include_context_complexity,
    )
    expected_execution = {
        "census_filter_join_dedup_before_lean": True,
        "inventory_uses_lean": False,
        "lean_workers_after_inventory": 2,
        "host_rss_ceiling_gib": 40,
        "elab_async": False,
    }
    for key, execution_value in expected_execution.items():
        _require_equal(execution.get(key), execution_value, f"execution.{key}")
    expected_release = {
        "destination": "Lemmy00/leanfaith-sft1-deterministic-v1",
        "prefix": "wave5/compiler_core_v1",
        "private_first": True,
        "model_facing_fields": ["reference", "candidate", "label"],
        "proof_certified_core_only": True,
    }
    for key, release_value in expected_release.items():
        _require_equal(release.get(key), release_value, f"release.{key}")
    verify_input_hashes = inventory.get("verify_input_file_hashes")
    if type(verify_input_hashes) is not bool:
        raise CompilerInventoryError("inventory.verify_input_file_hashes must be boolean")
    return InventorySettings(
        release_root=_resolve_path(repo_root, source.get("local_release_root"), "release root"),
        manifest_path=_resolve_path(repo_root, source.get("manifest_path"), "manifest path"),
        publication_receipt_path=_resolve_path(
            repo_root, source.get("publication_receipt_path"), "publication receipt"
        ),
        output_root=_resolve_path(repo_root, inventory.get("output_root"), "output root"),
        gold_blocklist_path=_resolve_path(
            repo_root, contamination.get("gold_blocklist_path"), "gold blocklist"
        ),
        gold_blocklist_sha256=str(contamination["gold_blocklist_sha256"]),
        pin=pin,
        project=project_context,
        audit_sample=audit_settings,
        config_sha256=hash_file(path),
        splits=tuple(cast(list[str], splits_value)),
        output_shards=int(inventory["output_shards"]),
        batch_rows=int(inventory["batch_rows"]),
        verify_input_file_hashes=verify_input_hashes,
    )


def _validate_publication_receipt(settings: InventorySettings) -> None:
    observed = hash_file(settings.publication_receipt_path)
    _require_equal(
        observed,
        settings.pin.publication_receipt_sha256,
        "publication receipt SHA-256",
    )
    receipt = _read_json(settings.publication_receipt_path)
    expected = {
        "artifact_kind": "cpt2_private_publication_receipt",
        "repo_id": settings.pin.repo_id,
        "provenance_commit": settings.pin.final_revision,
        "data_commit": settings.pin.data_commit,
        "finalized_manifest_sha256": settings.pin.manifest_sha256,
        "release_tree_sha256": settings.pin.release_tree_sha256,
        "private": True,
        "provenance_commit_is_head": True,
        "provenance_parent_is_data_commit": True,
        "remote_manifest_byte_identical": True,
        "local_remote_parquet_hashes_match": True,
        "parquet_lfs_hashes_unchanged_from_data_commit": True,
    }
    for key, value in expected.items():
        _require_equal(receipt.get(key), value, f"publication receipt {key}")


def load_pinned_input_shards(settings: InventorySettings) -> tuple[InputShard, ...]:
    """Validate the pinned manifest/receipt and return deterministic local inputs."""

    observed_manifest_sha = hash_file(settings.manifest_path)
    _require_equal(observed_manifest_sha, settings.pin.manifest_sha256, "CPT2 manifest SHA-256")
    _validate_publication_receipt(settings)
    manifest = _read_json(settings.manifest_path)
    expected_top = {
        "artifact_kind": "cpt2_full_release",
        "schema_version": "cpt2_theorem_body_label_v1",
        "scale_version": "cpt2_full_scale_v1",
        "run_id": settings.pin.cpt2_run_id,
        "output_rows": settings.pin.expected_release_rows,
    }
    for key, value in expected_top.items():
        _require_equal(manifest.get(key), value, f"CPT2 manifest {key}")
    output_labels = _mapping(manifest.get("output_labels"), "CPT2 output_labels")
    _require_equal(output_labels.get("true"), settings.pin.expected_valid_rows, "CPT2 valid rows")
    source = _mapping(manifest.get("source"), "CPT2 source")
    expected_source = {
        "repo_id": settings.pin.source_repo_id,
        "resolved_revision": settings.pin.source_revision,
        "parquet_path": settings.pin.source_parquet_path,
        "parquet_sha256": settings.pin.source_parquet_sha256,
        "schema": [
            ["source_code", "large_string"],
            ["validation", "large_string"],
            ["isValid", "bool"],
        ],
    }
    for key, value in expected_source.items():
        _require_equal(source.get(key), value, f"CPT2 source {key}")
    publication = _mapping(manifest.get("publication"), "CPT2 publication")
    _require_equal(publication.get("destination"), settings.pin.repo_id, "CPT2 destination")
    _require_equal(publication.get("data_commit"), settings.pin.data_commit, "CPT2 data commit")
    _require_equal(
        publication.get("data_files_release_tree_sha256"),
        settings.pin.release_tree_sha256,
        "CPT2 release tree",
    )
    splitter = _mapping(manifest.get("splitter"), "CPT2 splitter")
    _require_equal(splitter.get("method"), "declaration_aware_v3", "CPT2 splitter method")
    _require_equal(splitter.get("round_trip_failures"), 0, "CPT2 splitter round trips")
    _require_equal(splitter.get("scale_lean_rows"), 0, "CPT2 scale Lean rows")
    _require_equal(manifest.get("training_started"), False, "CPT2 training state")
    release = _mapping(manifest.get("release"), "CPT2 release")
    _require_equal(
        release.get("schema"),
        [["theorem", "large_string"], ["body", "large_string"], ["label", "bool"]],
        "CPT2 release schema",
    )
    shard_pairs = release.get("shards")
    if not isinstance(shard_pairs, list) or not shard_pairs:
        raise CompilerInventoryError("CPT2 release has no shard list")
    shards: list[InputShard] = []
    seen_files: set[str] = set()
    for pair_value in shard_pairs:
        pair = _mapping(pair_value, "CPT2 shard pair")
        part = int(pair["part"])
        total_parts = int(pair["total_parts"])
        for split in settings.splits:
            split_payload = _mapping(pair.get(split), f"CPT2 {split} shard")
            file = str(split_payload["file"])
            if Path(file).name != file or file in seen_files:
                raise CompilerInventoryError(f"unsafe or duplicate CPT2 shard file: {file}")
            seen_files.add(file)
            digest = str(split_payload["sha256"])
            if _HASH.fullmatch(digest) is None:
                raise CompilerInventoryError(f"malformed CPT2 shard SHA-256: {file}")
            labels = _mapping(split_payload.get("labels"), f"CPT2 {file} labels")
            shards.append(
                InputShard(
                    part=part,
                    total_parts=total_parts,
                    split=split,
                    file=file,
                    path=settings.release_root / file,
                    sha256=digest,
                    rows=int(split_payload["rows"]),
                    valid_rows=int(labels.get("true", 0)),
                )
            )
    shards.sort(key=lambda item: (item.part, settings.splits.index(item.split)))
    if sum(shard.rows for shard in shards) != settings.pin.expected_release_rows:
        raise CompilerInventoryError("selected CPT2 shards do not cover the pinned release rows")
    if sum(shard.valid_rows for shard in shards) != settings.pin.expected_valid_rows:
        raise CompilerInventoryError("selected CPT2 shards do not cover the pinned valid rows")
    return tuple(shards)


def _run_identity(
    settings: InventorySettings,
    shards: Sequence[InputShard],
    *,
    additional_contamination_hook_id: str | None,
    additional_contamination_hook_sha256: str | None,
) -> dict[str, object]:
    dependency_hashes: dict[str, str] = {}
    for label, module_file in (
        ("leanfaith.config.hashing", hashing_module.__file__),
        ("leanfaith.cpt2.splitters", cpt2_splitters.__file__),
        ("leanfaith.representations.views", representation_views.__file__),
    ):
        if module_file is None:
            raise CompilerInventoryError(f"cannot locate semantic dependency source: {label}")
        dependency_hashes[label] = hash_file(Path(module_file).resolve())
    return {
        "run_spec_version": RUN_SPEC_VERSION,
        "implementation_sha256": hash_file(Path(__file__).resolve()),
        "semantic_dependency_sha256": dependency_hashes,
        "config_sha256": settings.config_sha256,
        "inventory_schema_version": INVENTORY_SCHEMA_VERSION,
        "normalization_version": NORMALIZATION_VERSION,
        "feature_version": FEATURE_VERSION,
        "length_strata_version": LENGTH_STRATA_VERSION,
        "source_pin": settings.pin.to_dict(),
        "input_shards": [shard.identity() for shard in shards],
        "splits": list(settings.splits),
        "required_label": True,
        "project": settings.project.to_dict(),
        "audit_sample": (
            settings.audit_sample.to_dict() if settings.audit_sample is not None else None
        ),
        "gold_blocklist_sha256": settings.gold_blocklist_sha256,
        "additional_contamination_hook_id": additional_contamination_hook_id,
        "additional_contamination_hook_sha256": additional_contamination_hook_sha256,
        "output_shards": settings.output_shards,
        "record_payload": "content_and_locator_hashes_without_source_text",
        "dedup_order": [
            "exact_theorem_prefix",
            "context_qualified_normalized_name_free_signature",
        ],
        "representative_order": ["body_characters", "body_sha256", "source_row_id"],
    }


def _ensure_run_spec(settings: InventorySettings, identity: Mapping[str, object]) -> str:
    run_id = hash_canonical(dict(identity))
    payload = {"run_id": run_id, **identity}
    path = settings.output_root / "_state" / "run_spec.json"
    expected = canonical_json_bytes(payload) + b"\n"
    if path.is_file():
        if path.read_bytes() != expected:
            raise CompilerInventoryError("existing Wave 5 inventory run spec differs")
    else:
        write_atomic(path, expected)
    return run_id


def _connect_index(path: Path, run_id: str) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS processed_shards (
          shard_id TEXT PRIMARY KEY,
          receipt_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS raw_exact_groups (
          exact_hash TEXT PRIMARY KEY
        );
        CREATE TABLE IF NOT EXISTS exact_groups (
          exact_hash TEXT PRIMARY KEY,
          normalized_hash TEXT NOT NULL,
          winner_priority TEXT NOT NULL,
          member_count INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS normalized_groups (
          normalized_key TEXT PRIMARY KEY,
          normalized_hash TEXT NOT NULL,
          context_fingerprint TEXT NOT NULL,
          output_partition INTEGER NOT NULL,
          winner_exact_hash TEXT NOT NULL,
          winner_priority TEXT NOT NULL,
          record_json TEXT NOT NULL,
          exact_group_count INTEGER NOT NULL,
          member_count INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS normalized_partition_idx
          ON normalized_groups(output_partition, normalized_key);
        CREATE INDEX IF NOT EXISTS normalized_context_idx
          ON normalized_groups(context_fingerprint);
        CREATE TABLE IF NOT EXISTS context_counts (
          context_fingerprint TEXT PRIMARY KEY,
          group_count INTEGER NOT NULL
        );
        """
    )
    existing = connection.execute("SELECT value FROM meta WHERE key='run_id'").fetchone()
    if existing is None:
        connection.execute("INSERT INTO meta(key, value) VALUES('run_id', ?)", (run_id,))
        connection.commit()
    elif existing[0] != run_id:
        connection.close()
        raise CompilerInventoryError("compiler inventory index belongs to another run")
    return connection


def _input_receipt_path(settings: InventorySettings, shard: InputShard) -> Path:
    return settings.output_root / "_state" / "input_shards" / f"{shard.shard_id}.json"


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    write_atomic(path, canonical_json_bytes(dict(payload)) + b"\n")


def _validate_input_file(shard: InputShard) -> pq.ParquetFile:
    if not shard.path.is_file():
        raise CompilerInventoryError(f"missing pinned CPT2 shard: {shard.path}")
    observed = hash_file(shard.path)
    _require_equal(observed, shard.sha256, f"CPT2 shard {shard.file} SHA-256")
    parquet = pq.ParquetFile(shard.path)
    if parquet.schema_arrow != _RELEASE_SCHEMA:
        raise CompilerInventoryError(f"CPT2 shard schema drift: {shard.file}")
    if parquet.metadata.num_rows != shard.rows:
        raise CompilerInventoryError(f"CPT2 shard row-count drift: {shard.file}")
    return parquet


def _winner_priority(record: Mapping[str, object]) -> str:
    hashes = cast(dict[str, object], record["hashes"])
    lengths = cast(dict[str, object], record["lengths"])
    body_characters = lengths["body_characters"]
    if not isinstance(body_characters, int):
        raise CompilerInventoryError("indexed body length is not an integer")
    return f"{body_characters:012d}:{hashes['body_sha256']}:{record['source_row_id']}"


def _insert_record(
    connection: sqlite3.Connection,
    draft: CompilerRecordDraft,
    *,
    output_shards: int,
) -> tuple[bool, bool]:
    record = draft.record
    hashes = cast(dict[str, object], record["hashes"])
    exact_hash = str(hashes["theorem_sha256"])
    normalized_hash = str(hashes["normalized_signature_sha256"])
    normalized_key = str(record["normalized_group_id"])
    context = cast(dict[str, object], record["context"])
    context_fingerprint = str(context["context_fingerprint"])
    priority = _winner_priority(record)
    record_json = canonical_json_bytes(record).decode("utf-8")
    exact = connection.execute(
        "SELECT normalized_hash, winner_priority FROM exact_groups WHERE exact_hash=?",
        (exact_hash,),
    ).fetchone()
    if exact is None:
        connection.execute(
            "INSERT INTO exact_groups VALUES(?, ?, ?, 1)",
            (exact_hash, normalized_hash, priority),
        )
        exact_new = True
    else:
        if exact[0] != normalized_hash:
            raise CompilerInventoryError("one exact theorem prefix produced two normalized hashes")
        if priority < exact[1]:
            connection.execute(
                "UPDATE exact_groups SET winner_priority=?, "
                "member_count=member_count+1 WHERE exact_hash=?",
                (priority, exact_hash),
            )
        else:
            connection.execute(
                "UPDATE exact_groups SET member_count=member_count+1 WHERE exact_hash=?",
                (exact_hash,),
            )
        exact_new = False
    normalized = connection.execute(
        "SELECT winner_priority, normalized_hash, context_fingerprint "
        "FROM normalized_groups WHERE normalized_key=?",
        (normalized_key,),
    ).fetchone()
    if normalized is None:
        partition = int(normalized_key[:16], 16) % output_shards
        connection.execute(
            "INSERT INTO normalized_groups VALUES(?, ?, ?, ?, ?, ?, ?, 1, 1)",
            (
                normalized_key,
                normalized_hash,
                context_fingerprint,
                partition,
                exact_hash,
                priority,
                record_json,
            ),
        )
        normalized_new = True
    else:
        if normalized[1:] != (normalized_hash, context_fingerprint):
            raise CompilerInventoryError("contextual normalized signature hash collision")
        exact_increment = 1 if exact_new else 0
        if priority < normalized[0]:
            connection.execute(
                "UPDATE normalized_groups SET winner_exact_hash=?, winner_priority=?, "
                "record_json=?, exact_group_count=exact_group_count+?, "
                "member_count=member_count+1 WHERE normalized_key=?",
                (exact_hash, priority, record_json, exact_increment, normalized_key),
            )
        else:
            connection.execute(
                "UPDATE normalized_groups SET exact_group_count=exact_group_count+?, "
                "member_count=member_count+1 WHERE normalized_key=?",
                (exact_increment, normalized_key),
            )
        normalized_new = False
    return exact_new, normalized_new


def _combined_contamination_reason(
    draft: CompilerRecordDraft,
    hooks: Sequence[ContaminationHook],
) -> str | None:
    for hook in hooks:
        reason = hook(draft)
        if reason is not None:
            if not reason or any(char.isspace() for char in reason):
                raise CompilerInventoryError("contamination hooks must return stable token reasons")
            return reason
    return None


def _process_input_shard(
    *,
    connection: sqlite3.Connection,
    settings: InventorySettings,
    shard: InputShard,
    run_id: str,
    hooks: Sequence[ContaminationHook],
    journal: Journal,
) -> tuple[dict[str, Any], bool]:
    parquet = _validate_input_file(shard)
    existing = connection.execute(
        "SELECT receipt_json FROM processed_shards WHERE shard_id=?", (shard.shard_id,)
    ).fetchone()
    if existing is not None:
        receipt_value = json.loads(cast(str, existing[0]))
        existing_receipt = _mapping(receipt_value, f"receipt for {shard.file}")
        _require_equal(existing_receipt.get("run_id"), run_id, f"receipt run for {shard.file}")
        _require_equal(
            existing_receipt.get("input"), shard.identity(), f"receipt input {shard.file}"
        )
        _write_json(_input_receipt_path(settings, shard), existing_receipt)
        return existing_receipt, True

    counters: Counter[str] = Counter()
    parse_failures: Counter[str] = Counter()
    contamination_rejections: Counter[str] = Counter()
    row_index = 0
    connection.execute("BEGIN IMMEDIATE")
    try:
        for batch in parquet.iter_batches(
            batch_size=settings.batch_rows, columns=["theorem", "body", "label"]
        ):
            table = pa.Table.from_batches([batch], schema=_RELEASE_SCHEMA)
            theorems = table.column("theorem").to_pylist()
            bodies = table.column("body").to_pylist()
            labels = table.column("label").to_pylist()
            for theorem, body, label in zip(theorems, bodies, labels, strict=True):
                counters["input_rows"] += 1
                if (
                    not isinstance(theorem, str)
                    or not isinstance(body, str)
                    or type(label) is not bool
                ):
                    raise CompilerInventoryError(f"CPT2 row type/null drift in {shard.file}")
                if label is not True:
                    counters["false_filtered_rows"] += 1
                    row_index += 1
                    continue
                counters["valid_rows"] += 1
                theorem_sha = sha256_hex(theorem.encode("utf-8"))
                raw_exact_new = connection.execute(
                    "INSERT OR IGNORE INTO raw_exact_groups(exact_hash) VALUES(?)",
                    (theorem_sha,),
                ).rowcount
                counters["raw_exact_groups_new"] += raw_exact_new
                try:
                    draft = build_compiler_record(
                        theorem=theorem,
                        body=body,
                        row_index=row_index,
                        shard=shard,
                        pin=settings.pin,
                        project=settings.project,
                    )
                except CompilerRowRejected as error:
                    parse_failures[error.reason] += 1
                    row_index += 1
                    continue
                contamination_reason = _combined_contamination_reason(draft, hooks)
                if contamination_reason is not None:
                    contamination_rejections[contamination_reason] += 1
                    row_index += 1
                    continue
                exact_new, normalized_new = _insert_record(
                    connection, draft, output_shards=settings.output_shards
                )
                counters["accepted_rows"] += 1
                counters["exact_groups_new"] += int(exact_new)
                counters["normalized_groups_new"] += int(normalized_new)
                row_index += 1
        if row_index != shard.rows or counters["input_rows"] != shard.rows:
            raise CompilerInventoryError(f"CPT2 shard iteration count drift: {shard.file}")
        if counters["valid_rows"] != shard.valid_rows:
            raise CompilerInventoryError(f"CPT2 shard valid count drift: {shard.file}")
        rejected_rows = sum(parse_failures.values()) + sum(contamination_rejections.values())
        if counters["valid_rows"] != counters["accepted_rows"] + rejected_rows:
            raise CompilerInventoryError(f"valid row accounting drift: {shard.file}")
        receipt: dict[str, Any] = {
            "artifact_kind": "sft1_cpt2_inventory_input_shard",
            "run_id": run_id,
            "input": shard.identity(),
            "counts": dict(sorted(counters.items())),
            "parse_failures": dict(sorted(parse_failures.items())),
            "contamination_rejections": dict(sorted(contamination_rejections.items())),
        }
        receipt_json = canonical_json_bytes(receipt).decode("utf-8")
        connection.execute(
            "INSERT INTO processed_shards(shard_id, receipt_json) VALUES(?, ?)",
            (shard.shard_id, receipt_json),
        )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    _write_json(_input_receipt_path(settings, shard), receipt)
    journal.append(
        {
            "event": "input_shard_complete",
            "run_id": run_id,
            "shard_id": shard.shard_id,
            "file": shard.file,
            "counts": receipt["counts"],
            "parse_failures": receipt["parse_failures"],
            "contamination_rejections": receipt["contamination_rejections"],
        }
    )
    return receipt, False


def _output_paths(settings: InventorySettings, part: int) -> tuple[Path, Path]:
    data = (
        settings.output_root
        / "inventory"
        / f"part-{part:05d}-of-{settings.output_shards:05d}.jsonl"
    )
    receipt = settings.output_root / "_state" / "output_shards" / f"part-{part:05d}.json"
    return data, receipt


def _verify_output_receipt(
    settings: InventorySettings, part: int, run_id: str
) -> dict[str, Any] | None:
    data_path, receipt_path = _output_paths(settings, part)
    if not receipt_path.is_file():
        return None
    receipt = _read_json(receipt_path)
    _require_equal(receipt.get("run_id"), run_id, f"output shard {part} run")
    _require_equal(receipt.get("part"), part, f"output shard {part} part")
    if not data_path.is_file():
        raise CompilerInventoryError(f"output shard receipt has no data: {data_path}")
    _require_equal(hash_file(data_path), receipt.get("sha256"), f"output shard {part} hash")
    with data_path.open("rb") as handle:
        rows = sum(1 for line in handle if line.rstrip(b"\n"))
    _require_equal(rows, receipt.get("rows"), f"output shard {part} rows")
    return receipt


def _materialized_record(
    record_json: str,
    *,
    normalized_exact_groups: int,
    normalized_proofs: int,
    winner_exact_proofs: int,
) -> dict[str, Any]:
    value = json.loads(record_json)
    record = _mapping(value, "indexed compiler inventory record")
    record["dedup"] = {
        "winner_exact_proof_count": winner_exact_proofs,
        "normalized_exact_group_count": normalized_exact_groups,
        "normalized_proof_count": normalized_proofs,
    }
    record_without_hash = dict(record)
    record["inventory_record_sha256"] = hash_canonical(record_without_hash)
    return record


def _write_output_shard(
    *,
    connection: sqlite3.Connection,
    settings: InventorySettings,
    part: int,
    run_id: str,
    journal: Journal,
) -> tuple[dict[str, Any], bool]:
    existing = _verify_output_receipt(settings, part, run_id)
    if existing is not None:
        return existing, True
    data_path, receipt_path = _output_paths(settings, part)
    data_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = data_path.with_name(f".{data_path.name}.{os.getpid()}.partial")
    digest = hashlib.sha256()
    rows = 0
    feature_counts: Counter[str] = Counter()
    length_counts: Counter[str] = Counter()
    try:
        with temporary.open("wb") as handle:
            cursor = connection.execute(
                "SELECT n.normalized_key, n.record_json, n.exact_group_count, "
                "n.member_count, e.member_count "
                "FROM normalized_groups n JOIN exact_groups e "
                "ON e.exact_hash=n.winner_exact_hash "
                "WHERE n.output_partition=? ORDER BY n.normalized_key",
                (part,),
            )
            for _, record_json, exact_groups, total_members, winner_exact_members in cursor:
                record = _materialized_record(
                    cast(str, record_json),
                    normalized_exact_groups=int(exact_groups),
                    normalized_proofs=int(total_members),
                    winner_exact_proofs=int(winner_exact_members),
                )
                line = canonical_json_bytes(record) + b"\n"
                handle.write(line)
                digest.update(line)
                rows += 1
                feature_counts.update(cast(list[str], record["features"]))
                lengths = cast(dict[str, object], record["lengths"])
                for key in (
                    "theorem_stratum",
                    "body_stratum",
                    "full_source_stratum",
                    "signature_stratum",
                ):
                    length_counts[f"{key}:{lengths[key]}"] += 1
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, data_path)
    finally:
        temporary.unlink(missing_ok=True)
    receipt: dict[str, Any] = {
        "artifact_kind": "sft1_cpt2_inventory_output_shard",
        "run_id": run_id,
        "part": part,
        "total_parts": settings.output_shards,
        "file": data_path.name,
        "rows": rows,
        "bytes": data_path.stat().st_size,
        "sha256": digest.hexdigest(),
        "feature_counts": dict(sorted(feature_counts.items())),
        "length_strata_counts": dict(sorted(length_counts.items())),
    }
    _write_json(receipt_path, receipt)
    journal.append(
        {
            "event": "output_shard_complete",
            "run_id": run_id,
            "part": part,
            "rows": rows,
            "sha256": receipt["sha256"],
        }
    )
    return receipt, False


def _ensure_context_counts(connection: sqlite3.Connection, normalized_groups: int) -> None:
    row = connection.execute("SELECT COALESCE(SUM(group_count), 0) FROM context_counts").fetchone()
    if int(row[0]) == normalized_groups:
        return
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute("DELETE FROM context_counts")
        connection.execute(
            "INSERT INTO context_counts(context_fingerprint, group_count) "
            "SELECT context_fingerprint, COUNT(*) FROM normalized_groups "
            "GROUP BY context_fingerprint"
        )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    observed = connection.execute(
        "SELECT COALESCE(SUM(group_count), 0) FROM context_counts"
    ).fetchone()
    if int(observed[0]) != normalized_groups:
        raise CompilerInventoryError("context-frequency index does not cover inventory rows")


def _context_frequency_stratum(count: int) -> str:
    if count == 1:
        return "singleton"
    if count <= 4:
        return "rare_2_4"
    if count <= 24:
        return "medium_5_24"
    return "common_25_plus"


def _audit_cells(
    record: Mapping[str, Any],
    settings: AuditSampleSettings,
    *,
    context_count: int,
) -> tuple[str, ...]:
    configured_features = set(settings.features)
    features = sorted(configured_features & set(cast(list[str], record["features"])))
    lengths = _mapping(record["lengths"], "audit record lengths")
    cells: list[str] = [f"feature:{feature}" for feature in features]
    for dimension in settings.length_dimensions:
        cells.append(f"length:{dimension}:{lengths[f'{dimension}_stratum']}")
    if "signature" in settings.length_dimensions and "full_source" in settings.length_dimensions:
        signature_length = lengths["signature_stratum"]
        source_length = lengths["full_source_stratum"]
        cells.extend(f"joint:{feature}:{signature_length}:{source_length}" for feature in features)
    if settings.include_context_frequency_strata:
        cells.append(f"context_frequency:{_context_frequency_stratum(context_count)}")
    context = _mapping(record["context"], "audit record context")
    if settings.include_namespace_status_strata:
        cells.append(f"namespace_status:{context['namespace_status']}")
    if settings.include_context_complexity_strata:
        cells.append(f"context_complexity:{context['context_complexity']}")
    return tuple(cells)


def _audit_paths(settings: InventorySettings) -> tuple[Path, Path]:
    if settings.audit_sample is None:
        raise ValueError("audit paths require configured sample settings")
    data_path = settings.output_root / "audit" / f"sample-{settings.audit_sample.size:05d}.jsonl"
    receipt_path = settings.output_root / "_state" / "audit_sample.json"
    return data_path, receipt_path


def _verify_audit_receipt(settings: InventorySettings, run_id: str) -> dict[str, Any] | None:
    if settings.audit_sample is None:
        return None
    data_path, receipt_path = _audit_paths(settings)
    if not receipt_path.is_file():
        return None
    receipt = _read_json(receipt_path)
    _require_equal(receipt.get("run_id"), run_id, "audit sample run")
    _require_equal(receipt.get("policy"), settings.audit_sample.to_dict(), "audit sample policy")
    if not data_path.is_file():
        raise CompilerInventoryError("audit sample receipt has no data file")
    _require_equal(hash_file(data_path), receipt.get("sha256"), "audit sample SHA-256")
    with data_path.open("rb") as handle:
        rows = sum(1 for line in handle if line.rstrip(b"\n"))
    _require_equal(rows, receipt.get("rows"), "audit sample rows")
    return receipt


def _write_audit_sample(
    *,
    connection: sqlite3.Connection,
    settings: InventorySettings,
    run_id: str,
    population: int,
    journal: Journal,
) -> tuple[dict[str, Any] | None, bool]:
    sample = settings.audit_sample
    if sample is None:
        return None, False
    existing = _verify_audit_receipt(settings, run_id)
    if existing is not None:
        return existing, True
    _ensure_context_counts(connection, population)
    best_by_cell: dict[str, tuple[str, str, dict[str, Any], int]] = {}
    global_capacity = sample.size * 3 + 2048
    global_heap: list[tuple[int, str, dict[str, Any], int]] = []
    population_namespace_status_counts: Counter[str] = Counter()
    population_context_complexity_counts: Counter[str] = Counter()
    cursor = connection.execute(
        "SELECT n.normalized_key, n.record_json, n.exact_group_count, n.member_count, "
        "e.member_count, c.group_count FROM normalized_groups n "
        "JOIN exact_groups e ON e.exact_hash=n.winner_exact_hash "
        "JOIN context_counts c ON c.context_fingerprint=n.context_fingerprint "
        "ORDER BY n.normalized_key"
    )
    for key_value, record_json, exact_groups, proofs, winner_proofs, context_count in cursor:
        key = str(key_value)
        record = _materialized_record(
            cast(str, record_json),
            normalized_exact_groups=int(exact_groups),
            normalized_proofs=int(proofs),
            winner_exact_proofs=int(winner_proofs),
        )
        context = _mapping(record["context"], "audit record context")
        population_namespace_status_counts[str(context["namespace_status"])] += 1
        population_context_complexity_counts[str(context["context_complexity"])] += 1
        count = int(context_count)
        for cell in _audit_cells(record, sample, context_count=count):
            score = hash_canonical([sample.salt, "cell", cell, key])
            current = best_by_cell.get(cell)
            candidate = (score, key, record, count)
            if current is None or candidate[:2] < current[:2]:
                best_by_cell[cell] = candidate
        global_score = int(hash_canonical([sample.salt, "fill", key]), 16)
        heap_entry = (-global_score, key, record, count)
        if len(global_heap) < global_capacity:
            heapq.heappush(global_heap, heap_entry)
        else:
            worst_score = -global_heap[0][0]
            if (global_score, key) < (worst_score, global_heap[0][1]):
                heapq.heapreplace(global_heap, heap_entry)

    selected: dict[str, tuple[dict[str, Any], int]] = {}

    def cell_priority(cell: str) -> tuple[int, str]:
        mandatory = cell.startswith(("namespace_status:", "context_complexity:"))
        return (0 if mandatory else 1, cell)

    for cell in sorted(best_by_cell, key=cell_priority):
        _, key, record, context_count = best_by_cell[cell]
        if len(selected) >= sample.size:
            break
        selected.setdefault(key, (record, context_count))
    global_candidates = sorted(
        ((-score, key, record, count) for score, key, record, count in global_heap),
        key=lambda item: (item[0], item[1]),
    )
    for _, key, record, context_count in global_candidates:
        if len(selected) >= sample.size:
            break
        selected.setdefault(key, (record, context_count))
    expected_rows = min(sample.size, population)
    if len(selected) != expected_rows:
        raise CompilerInventoryError(
            f"audit sample underfilled: expected {expected_rows}, got {len(selected)}"
        )
    selected_cells = {
        cell
        for record, context_count in selected.values()
        for cell in _audit_cells(record, sample, context_count=context_count)
    }
    required_context_cells = {
        cell
        for cell in best_by_cell
        if cell.startswith(("namespace_status:", "context_complexity:"))
    }
    missing_context_cells = sorted(required_context_cells - selected_cells)
    if missing_context_cells:
        raise CompilerInventoryError(
            "audit sample cannot cover required context-reconstruction cells: "
            f"{missing_context_cells}"
        )
    ordered = sorted(
        selected.items(),
        key=lambda item: (hash_canonical([sample.salt, "order", item[0]]), item[0]),
    )
    data_path, receipt_path = _audit_paths(settings)
    data_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = data_path.with_name(f".{data_path.name}.{os.getpid()}.partial")
    digest = hashlib.sha256()
    covered_cells: set[str] = set()
    feature_counts: Counter[str] = Counter()
    context_frequency_counts: Counter[str] = Counter()
    namespace_status_counts: Counter[str] = Counter()
    context_complexity_counts: Counter[str] = Counter()
    try:
        with temporary.open("wb") as handle:
            for key, (record, context_count) in ordered:
                cells = _audit_cells(record, sample, context_count=context_count)
                covered_cells.update(cells)
                feature_counts.update(cast(list[str], record["features"]))
                context_frequency = _context_frequency_stratum(context_count)
                context_frequency_counts[context_frequency] += 1
                context = _mapping(record["context"], "audit record context")
                namespace_status_counts[str(context["namespace_status"])] += 1
                context_complexity_counts[str(context["context_complexity"])] += 1
                sample_record = {
                    **record,
                    "audit_selection": {
                        "sample_version": AUDIT_SAMPLE_VERSION,
                        "sample_salt": sample.salt,
                        "normalized_group_id": key,
                        "cells": list(cells),
                        "context_group_count": context_count,
                        "context_frequency_stratum": context_frequency,
                    },
                }
                line = canonical_json_bytes(sample_record) + b"\n"
                handle.write(line)
                digest.update(line)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, data_path)
    finally:
        temporary.unlink(missing_ok=True)
    available_features = sorted(
        cell.removeprefix("feature:") for cell in best_by_cell if cell.startswith("feature:")
    )
    missing_selected_features = sorted(set(available_features) - set(feature_counts))
    unavailable_configured_features = sorted(set(sample.features) - set(available_features))
    if sample.size >= len(available_features) and missing_selected_features:
        raise CompilerInventoryError(
            f"audit sample omitted available features: {missing_selected_features}"
        )
    receipt: dict[str, Any] = {
        "artifact_kind": "sft1_cpt2_inventory_audit_sample",
        "run_id": run_id,
        "policy": sample.to_dict(),
        "population_rows": population,
        "rows": len(ordered),
        "complete_population": len(ordered) == population,
        "file": data_path.name,
        "bytes": data_path.stat().st_size,
        "sha256": digest.hexdigest(),
        "covered_cells": sorted(covered_cells),
        "available_configured_features": available_features,
        "unavailable_configured_features": unavailable_configured_features,
        "missing_selected_features": missing_selected_features,
        "feature_counts": dict(sorted(feature_counts.items())),
        "context_frequency_counts": dict(sorted(context_frequency_counts.items())),
        "population_namespace_status_counts": dict(
            sorted(population_namespace_status_counts.items())
        ),
        "population_context_complexity_counts": dict(
            sorted(population_context_complexity_counts.items())
        ),
        "namespace_status_counts": dict(sorted(namespace_status_counts.items())),
        "context_complexity_counts": dict(sorted(context_complexity_counts.items())),
        "missing_required_context_cells": missing_context_cells,
    }
    _write_json(receipt_path, receipt)
    journal.append(
        {
            "event": "audit_sample_complete",
            "run_id": run_id,
            "rows": len(ordered),
            "sha256": receipt["sha256"],
        }
    )
    return receipt, False


def _count(connection: sqlite3.Connection, table: str) -> int:
    allowed = {"raw_exact_groups", "exact_groups", "normalized_groups", "processed_shards"}
    if table not in allowed:
        raise ValueError(f"unsupported inventory count table: {table}")
    row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    return int(row[0])


def _global_normalized_count(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT COUNT(DISTINCT normalized_hash) FROM normalized_groups"
    ).fetchone()
    return int(row[0])


def _sum_receipt_counts(receipts: Sequence[Mapping[str, Any]], field: str) -> dict[str, int]:
    total: Counter[str] = Counter()
    for receipt in receipts:
        values = _mapping(receipt.get(field, {}), f"receipt {field}")
        total.update({str(key): int(value) for key, value in values.items()})
    return dict(sorted(total.items()))


def _manifest_payload(
    *,
    settings: InventorySettings,
    run_id: str,
    shards: Sequence[InputShard],
    input_receipts: Sequence[Mapping[str, Any]],
    output_receipts: Sequence[Mapping[str, Any]],
    raw_exact_groups: int,
    exact_groups: int,
    normalized_groups: int,
    global_normalized_signatures: int,
    additional_contamination_hook_id: str | None,
    additional_contamination_hook_sha256: str | None,
    audit_receipt: Mapping[str, Any] | None,
) -> dict[str, object]:
    counts = _sum_receipt_counts(input_receipts, "counts")
    parse_failures = _sum_receipt_counts(input_receipts, "parse_failures")
    contamination_rejections = _sum_receipt_counts(input_receipts, "contamination_rejections")
    feature_counts = _sum_receipt_counts(output_receipts, "feature_counts")
    length_counts = _sum_receipt_counts(output_receipts, "length_strata_counts")
    output_rows = sum(int(receipt["rows"]) for receipt in output_receipts)
    if output_rows != normalized_groups:
        raise CompilerInventoryError("output shards do not cover normalized inventory groups")
    output_tree_sha256 = hash_canonical(
        [
            {"part": int(receipt["part"]), "sha256": str(receipt["sha256"])}
            for receipt in output_receipts
        ]
    )
    return {
        "artifact_kind": "sft1_cpt2_compiler_inventory",
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "run_id": run_id,
        "source_pin": settings.pin.to_dict(),
        "source_manifest_path": str(settings.manifest_path),
        "publication_receipt_path": str(settings.publication_receipt_path),
        "splits": list(settings.splits),
        "required_label": True,
        "project": settings.project.to_dict(),
        "normalization": {
            "version": NORMALIZATION_VERSION,
            "scope": "final theorem or lemma signature with declaration name removed",
            "preserves": ["string_literals", "character_literals", "guillemet_identifiers"],
            "does_not_perform": ["alpha_normalization", "notation_rewriting", "semantic_dedup"],
        },
        "deduplication": {
            "first_key": "sha256(exact theorem prefix)",
            "second_key": "hash(normalized name-free signature, context fingerprint)",
            "global_normalized_text_hashes": "diagnostic_only_not_cross_context_dedup",
            "representative_order": ["body_characters", "body_sha256", "source_row_id"],
        },
        "contamination": {
            "gold_blocklist_sha256": settings.gold_blocklist_sha256,
            "additional_hook_id": additional_contamination_hook_id,
            "additional_hook_sha256": additional_contamination_hook_sha256,
            "action": "excluded_before_dedup",
            "rejections": contamination_rejections,
        },
        "failures": {"signature_or_context_parse": parse_failures},
        "counts": {
            **counts,
            "raw_valid_exact_prefixes": raw_exact_groups,
            "post_screen_exact_prefixes": exact_groups,
            "normalized_unique_contextual_signatures": normalized_groups,
            "global_normalized_text_signatures": global_normalized_signatures,
            "exact_duplicate_valid_rows": counts.get("accepted_rows", 0) - exact_groups,
            "normalized_duplicate_exact_prefixes": exact_groups - normalized_groups,
            "output_rows": output_rows,
        },
        "features": {"version": FEATURE_VERSION, "counts": feature_counts},
        "length_strata": {"version": LENGTH_STRATA_VERSION, "counts": length_counts},
        "audit_sample": dict(audit_receipt) if audit_receipt is not None else None,
        "input_shards": [shard.identity() for shard in shards],
        "output": {
            "format": "canonical_jsonl",
            "source_text_storage": "locator_and_hashes_only",
            "partition": "contextual_normalized_group_id_mod_output_shards",
            "tree_sha256": output_tree_sha256,
            "shards": [dict(receipt) for receipt in output_receipts],
        },
        "durability": {
            "input": "one SQLite transaction and manifest-last receipt per source shard",
            "output": "atomic JSONL plus manifest-last receipt per hash partition",
            "journal": str(settings.output_root / "_state" / "journal.jsonl"),
            "aggregate_manifest": "written last",
        },
        "lean_calls": 0,
    }


def build_inventory(
    settings: InventorySettings,
    *,
    contamination_hook: ContaminationHook | None = None,
    contamination_hook_id: str | None = None,
    contamination_hook_sha256: str | None = None,
) -> InventoryResult:
    """Build or resume the pinned zero-Lean compiler inventory."""

    hook_fields = (contamination_hook, contamination_hook_id, contamination_hook_sha256)
    if any(field is None for field in hook_fields) and any(
        field is not None for field in hook_fields
    ):
        raise ValueError(
            "an additional contamination hook, stable ID, and policy SHA-256 are all required"
        )
    if contamination_hook_id is not None and (
        not contamination_hook_id or any(char.isspace() for char in contamination_hook_id)
    ):
        raise ValueError("contamination_hook_id must be one non-empty stable token")
    if contamination_hook_sha256 is not None and (
        _HASH.fullmatch(contamination_hook_sha256) is None
    ):
        raise ValueError("contamination_hook_sha256 must be a lowercase SHA-256")
    shards = load_pinned_input_shards(settings)
    identity = _run_identity(
        settings,
        shards,
        additional_contamination_hook_id=contamination_hook_id,
        additional_contamination_hook_sha256=contamination_hook_sha256,
    )
    run_id = _ensure_run_spec(settings, identity)
    gold_hook = GoldenBlocklistHook.load(
        settings.gold_blocklist_path, settings.gold_blocklist_sha256
    )
    hooks: tuple[ContaminationHook, ...] = (
        (gold_hook,) if contamination_hook is None else (gold_hook, contamination_hook)
    )
    journal = Journal(settings.output_root / "_state" / "journal.jsonl")
    connection = _connect_index(settings.output_root / "_state" / "index.sqlite3", run_id)
    written_input = 0
    resumed_input = 0
    written_output = 0
    resumed_output = 0
    try:
        input_receipts: list[dict[str, Any]] = []
        for shard in shards:
            receipt, resumed = _process_input_shard(
                connection=connection,
                settings=settings,
                shard=shard,
                run_id=run_id,
                hooks=hooks,
                journal=journal,
            )
            input_receipts.append(receipt)
            resumed_input += int(resumed)
            written_input += int(not resumed)
        raw_exact_groups = _count(connection, "raw_exact_groups")
        if (
            settings.pin.expected_valid_exact_prefixes is not None
            and raw_exact_groups != settings.pin.expected_valid_exact_prefixes
        ):
            raise CompilerInventoryError(
                "valid exact-prefix count differs from the pinned CPT2 census: "
                f"expected {settings.pin.expected_valid_exact_prefixes}, got {raw_exact_groups}"
            )
        exact_groups = _count(connection, "exact_groups")
        normalized_groups = _count(connection, "normalized_groups")
        global_normalized_signatures = _global_normalized_count(connection)
        output_receipts: list[dict[str, Any]] = []
        for part in range(settings.output_shards):
            receipt, resumed = _write_output_shard(
                connection=connection,
                settings=settings,
                part=part,
                run_id=run_id,
                journal=journal,
            )
            output_receipts.append(receipt)
            resumed_output += int(resumed)
            written_output += int(not resumed)
        audit_receipt, _audit_resumed = _write_audit_sample(
            connection=connection,
            settings=settings,
            run_id=run_id,
            population=normalized_groups,
            journal=journal,
        )
        manifest = _manifest_payload(
            settings=settings,
            run_id=run_id,
            shards=shards,
            input_receipts=input_receipts,
            output_receipts=output_receipts,
            raw_exact_groups=raw_exact_groups,
            exact_groups=exact_groups,
            normalized_groups=normalized_groups,
            global_normalized_signatures=global_normalized_signatures,
            additional_contamination_hook_id=contamination_hook_id,
            additional_contamination_hook_sha256=contamination_hook_sha256,
            audit_receipt=audit_receipt,
        )
        manifest_path = settings.output_root / "manifest.json"
        manifest_bytes = canonical_json_bytes(manifest) + b"\n"
        if manifest_path.is_file() and manifest_path.read_bytes() != manifest_bytes:
            raise CompilerInventoryError("existing compiler inventory manifest differs")
        write_atomic(manifest_path, manifest_bytes)
    finally:
        connection.close()
    return InventoryResult(
        manifest_path=manifest_path,
        audit_sample_path=(
            _audit_paths(settings)[0] if settings.audit_sample is not None else None
        ),
        run_id=run_id,
        output_rows=normalized_groups,
        written_input_shards=written_input,
        resumed_input_shards=resumed_input,
        written_output_shards=written_output,
        resumed_output_shards=resumed_output,
    )


def iter_inventory_records(output_root: Path) -> Iterator[dict[str, Any]]:
    """Read completed inventory shards in deterministic partition order."""

    manifest = _read_json(output_root / "manifest.json")
    output = _mapping(manifest.get("output"), "inventory output")
    shards = output.get("shards")
    if not isinstance(shards, list):
        raise CompilerInventoryError("inventory manifest output shards are malformed")
    for receipt_value in sorted(
        shards, key=lambda value: int(_mapping(value, "output shard")["part"])
    ):
        receipt = _mapping(receipt_value, "output shard")
        path = output_root / "inventory" / str(receipt["file"])
        if hash_file(path) != receipt.get("sha256"):
            raise CompilerInventoryError(f"inventory output shard hash drift: {path.name}")
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                value = json.loads(line)
                yield _mapping(value, f"inventory row in {path.name}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    result = build_inventory(load_inventory_config(args.config))
    print(
        json.dumps(
            {
                "manifest": str(result.manifest_path),
                "run_id": result.run_id,
                "output_rows": result.output_rows,
                "written_input_shards": result.written_input_shards,
                "resumed_input_shards": result.resumed_input_shards,
                "written_output_shards": result.written_output_shards,
                "resumed_output_shards": result.resumed_output_shards,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
