"""Deterministic, Lean-free matched-500 source freeze for SFT2B.

This module consumes only already-frozen source and compilation evidence.  It
does not start Lean, load ReForm weights, invoke judges, or create labels.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pyarrow.parquet as pq  # type: ignore[import-untyped]
import yaml

from leanfaith.collect2.postprocess import GoldenBlocklist
from leanfaith.config.hashing import (
    canonical_json_bytes,
    hash_canonical,
    hash_file,
    sha256_hex,
)
from leanfaith.cpt2.splitters import mask_lean_source, split_declaration_aware
from leanfaith.representations.goal_v1 import CompileContext
from leanfaith.representations.views import (
    collapse_lean_whitespace,
    normalize_headless,
    signature_near_dup_hash,
)
from leanfaith.sft2b.lean import compile_context_from_source
from leanfaith.sft2b.pins import RuntimePins, verify_runtime_pins
from leanfaith.sft2b.reuse import load_existing_301
from leanfaith.sft2b.schemas import (
    CompileContextRecord,
    SourceProvenance,
    SourceRecord,
    stable_id,
)

SCHEMA_VERSION = "sft2b_reform_matched_500_bundle_v1"
PROMPT_COUNTS_SCHEMA_VERSION = "sft2b_prompt_token_counts_v1"
MANIFEST_SCHEMA_VERSION = "sft2b_source_manifest_v1"
EXPECTED_MIX = {
    "library_docstring": 175,
    "theorem_problem": 175,
    "broader_public_synthetic": 100,
    "specialist_high_difficulty": 50,
}
OUTPUT_NAMES = (
    "sources.jsonl",
    "prompt_token_counts.json",
    "source_manifest.json",
    "SHA256SUMS",
)

_FENCE = re.compile(r"```(?:lean4|lean)\s*\n(.*?)```", flags=re.DOTALL)
_DECLARATION_HEAD = re.compile(
    r"(?m)^[ \t]*(?:(?:private|protected|noncomputable|unsafe|local)\s+)*"
    r"(?:theorem|lemma)\s+(«[^»\n]+»|[^\s:({\[]+)"
)
_WORDS = re.compile(r"[A-Za-z]{2,}")
_SPACE = re.compile(r"\s+")
_MATHLIB_DOC_REJECT = re.compile(
    r"\b(?:above|below|the following|this theorem|this lemma|this result|variant|version|"
    r"convenience|wrapper|alias|implementation|deprecated|to_additive|helper|simp lemma|"
    r"see also|compare with|special case|more general)\b",
    flags=re.IGNORECASE,
)
_PROMPTISH_REJECT = re.compile(
    r"\b(?:autoformalize|formalize this|lean 4 code|fill in the proof|complete the theorem|"
    r"respond with|natural language statement is)\b",
    flags=re.IGNORECASE,
)
_SOLUTION_LEAK_REJECT = re.compile(
    r"(?:\\blacksquare|\bfirst we (?:use|prove|show)|\busing the same method\b|"
    r"\btherefore we (?:have|obtain|get|conclude)|\bsolution\s*:)",
    flags=re.IGNORECASE,
)


class SourceFreezeError(RuntimeError):
    """The frozen input or deterministic output contract drifted."""


@dataclass(frozen=True, slots=True)
class AuditedSource:
    source_class: str
    record: SourceRecord
    headless: str
    near_dup_hash: str
    problem_identity: str
    selection_group: str
    selection_hash: str
    complexity_score: int


@dataclass(frozen=True, slots=True)
class SourceAudit:
    audited_rows: int
    eligible_rows: int
    exclusion_counts: Mapping[str, int]

    def to_dict(self) -> dict[str, object]:
        return {
            "audited_rows": self.audited_rows,
            "eligible_rows": self.eligible_rows,
            "exclusion_counts": dict(sorted(self.exclusion_counts.items())),
        }


@dataclass(frozen=True, slots=True)
class FreezeResult:
    output_dir: Path
    rows: tuple[SourceRecord, ...]
    source_mix: Mapping[str, int]
    maximum_prompt_tokens: int
    required_max_model_len: int
    file_sha256: Mapping[str, str]


def _object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SourceFreezeError(f"expected a JSON object: {path}")
    return cast(dict[str, Any], value)


def _require_hash(path: Path, expected: object, label: str) -> str:
    if not path.is_file():
        raise SourceFreezeError(f"missing {label}: {path}")
    observed = hash_file(path)
    if observed != expected:
        raise SourceFreezeError(
            f"{label} hash mismatch: expected {expected!r}, observed {observed!r}"
        )
    return observed


def _canonical_line(value: object) -> bytes:
    return canonical_json_bytes(value) + b"\n"


def _read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise SourceFreezeError(f"non-object JSONL row at {path}:{line_number}")
            yield cast(dict[str, Any], value)


def _normalized_nl(value: str) -> str:
    return _SPACE.sub(" ", value).strip()


def _nl_key(value: str) -> str:
    return _normalized_nl(value).casefold()


def _standalone_nl(value: str, *, mathlib_docstring: bool) -> str | None:
    normalized = _normalized_nl(value)
    minimum = 30 if mathlib_docstring else 25
    maximum = 1000 if mathlib_docstring else 1500
    if not minimum <= len(normalized) <= maximum:
        return None
    if len(_WORDS.findall(normalized)) < 5:
        return None
    if _PROMPTISH_REJECT.search(normalized):
        return None
    if not mathlib_docstring and _SOLUTION_LEAK_REJECT.search(normalized):
        return None
    if mathlib_docstring:
        if _MATHLIB_DOC_REJECT.search(normalized) or re.search(
            r"\b(?:see|docstring|diagram)\b", normalized, flags=re.IGNORECASE
        ):
            return None
        if normalized[-1] not in ".?!":
            return None
        if normalized.count("`") > 12:
            return None
    return normalized


def _closed_proposition(headless: str) -> str:
    value = collapse_lean_whitespace(headless)
    depth = 0
    in_string = False
    in_guillemet = False
    escaped = False
    separator: int | None = None
    for index, char in enumerate(value):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if in_guillemet:
            if char == "»":
                in_guillemet = False
            continue
        if char == '"':
            in_string = True
        elif char == "«":
            in_guillemet = True
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
            if depth < 0:
                raise SourceFreezeError(f"unbalanced headless signature: {headless!r}")
        elif char == ":" and depth == 0:
            separator = index
            break
    if separator is None:
        raise SourceFreezeError(f"headless signature lacks result colon: {headless!r}")
    binders = value[:separator].strip()
    target = value[separator + 1 :].strip()
    if not target:
        raise SourceFreezeError("headless signature has an empty target")
    proposition = target if not binders else f"∀ {binders}, {target}"
    if "sorry" in proposition or ":= by" in proposition:
        raise SourceFreezeError("proof material leaked into a reference proposition")
    return proposition


def _block_comments(source: str) -> list[tuple[int, int, str]]:
    comments: list[tuple[int, int, str]] = []
    index = 0
    while index + 1 < len(source):
        if not source.startswith("/-", index):
            index += 1
            continue
        start = index
        index += 2
        depth = 1
        while index + 1 < len(source) and depth:
            if source.startswith("/-", index):
                depth += 1
                index += 2
            elif source.startswith("-/", index):
                depth -= 1
                index += 2
            else:
                index += 1
        if depth:
            raise SourceFreezeError("unterminated block comment in a selected source")
        body_start = start + (3 if source.startswith("/--", start) else 2)
        comments.append((start, index, source[body_start : index - 2]))
    return comments


def _last_declaration(source: str) -> tuple[str, str, int]:
    split = split_declaration_aware(source)
    if split is None:
        raise SourceFreezeError("cannot split final theorem declaration")
    matches = list(_DECLARATION_HEAD.finditer(mask_lean_source(split.theorem)))
    if not matches:
        raise SourceFreezeError("cannot locate final theorem declaration head")
    match = matches[-1]
    declaration = split.theorem[match.start() :] + "by sorry"
    return declaration, match.group(1), match.start()


def _parse_scalar(value: str) -> str | int | float | bool | None:
    stripped = value.strip()
    if stripped == "true":
        return True
    if stripped == "false":
        return False
    if re.fullmatch(r"-?[0-9]+", stripped):
        return int(stripped)
    if re.fullmatch(r"-?[0-9]+\.[0-9]+", stripped):
        return float(stripped)
    if re.fullmatch(r'"(?:[^"\\]|\\.)*"', stripped):
        return stripped[1:-1]
    return None


def _parse_header(
    header: str,
) -> tuple[str, tuple[str, ...], tuple[str, ...], dict[str, str | int | float | bool]] | None:
    imports: list[str] = []
    opens: list[str] = []
    scoped: list[str] = []
    options: dict[str, str | int | float | bool] = {}
    comments = _block_comments(header)
    visible = list(header)
    for start, finish, _ in comments:
        for index in range(start, finish):
            if visible[index] not in "\r\n":
                visible[index] = " "
    for raw_line in "".join(visible).splitlines():
        line = raw_line.split("--", 1)[0].strip()
        if not line:
            continue
        if re.fullmatch(r"(?:(?:public|meta)\s+)*import\s+\S+(?:\s+\S+)*", line):
            imports.append(line)
            continue
        if line.startswith("open scoped "):
            names = line.removeprefix("open scoped ").split()
            if not names:
                return None
            scoped.extend(names)
            continue
        if line.startswith("open "):
            names = line.removeprefix("open ").split()
            if not names:
                return None
            opens.extend(names)
            continue
        option_match = re.fullmatch(r"set_option\s+(\S+)\s+(.+)", line)
        if option_match is not None:
            option = _parse_scalar(option_match.group(2))
            if option is None:
                return None
            options[option_match.group(1)] = option
            continue
        return None
    if not imports:
        return None
    return "\n".join(imports) + "\n", tuple(opens), tuple(scoped), options


def _helper_body(helper_path: Path, expected_hash: str) -> str:
    _require_hash(helper_path, expected_hash, "SFT2B Lean helper")
    return "\n".join(
        line
        for line in helper_path.read_text(encoding="utf-8").splitlines()
        if not line.startswith("import ")
    )


def _context_record(
    *,
    import_header: str,
    open_context: tuple[str, ...],
    scoped_context: tuple[str, ...],
    options: Mapping[str, str | int | float | bool],
    source_context_path: Path,
    source_context_sha256: str,
    pins: RuntimePins,
    helper_path: Path,
    configured_source_context_id: str | None = None,
) -> CompileContextRecord:
    source_payload = {
        "schema_version": "sft2b_source_compile_context_v1",
        "project_id": "mathlib",
        "project_revision": "d568c8c09630de097a046763c17b9ea99f95f950",
        "lean_version": "v4.31.0-rc1",
        "import_header": import_header,
        "namespace_context": [],
        "open_context": list(open_context),
        "scoped_context": list(scoped_context),
        "options": dict(sorted(options.items())),
    }
    source_context_id = configured_source_context_id or f"ctx:{hash_canonical(source_payload)}"
    render_context = CompileContext(
        project_id="mathlib",
        project_revision="d568c8c09630de097a046763c17b9ea99f95f950",
        lean_version="v4.31.0-rc1",
        import_header=import_header,
        command_preamble=_helper_body(helper_path, pins.sft2b_helper_hash),
        open_context=open_context,
        scoped_context=scoped_context,
        options=options,
    )
    return CompileContextRecord(
        source_context_id=source_context_id,
        render_compile_context_id=render_context.compile_context_id,
        project_id="mathlib",
        project_revision="d568c8c09630de097a046763c17b9ea99f95f950",
        project_path="/storage/milikic/leanfaith/mathlib4",
        lean_version="v4.31.0-rc1",
        import_header=import_header,
        open_context=open_context,
        scoped_context=scoped_context,
        options=dict(options),
        source_context_path=str(source_context_path),
        source_context_sha256=source_context_sha256,
        helper_path=str(helper_path),
        helper_sha256=pins.sft2b_helper_hash,
    )


def _selection_hash(seed: str, source_class: str, key: str) -> str:
    return hash_canonical({"seed": seed, "source_class": source_class, "key": key})


def _record(
    *,
    nl: str,
    theorem_id: str,
    declaration_name: str | None,
    proposition: str,
    context: CompileContextRecord,
    provenance: SourceProvenance,
) -> SourceRecord:
    source_id = stable_id(
        "sft2b_source",
        {
            "reference_theorem_id": theorem_id,
            "nl_statement": nl,
            "source_revision": provenance.source_revision,
        },
    )
    return SourceRecord(
        source_id=source_id,
        nl_statement=nl,
        reference_theorem_id=theorem_id,
        reference_declaration_name=declaration_name,
        reference_proposition=proposition,
        reference_proposition_sha256=sha256_hex(proposition.encode("utf-8")),
        compile_context=context,
        provenance=provenance,
        standalone_nl=True,
        trusted_reference=True,
        training_eligible=True,
    )


def _golden_exact(path: Path) -> frozenset[str]:
    values: set[str] = set()
    for row in _read_jsonl(path):
        for key in ("reference_headless", "candidate_headless"):
            value = row.get(key)
            if isinstance(value, str) and value.strip():
                values.add(collapse_lean_whitespace(value))
    return frozenset(values)


def _mathlib_sources(
    config: Mapping[str, Any],
    *,
    repo_root: Path,
    config_hash: str,
    pins: RuntimePins,
    helper_path: Path,
    seed: str,
) -> tuple[list[AuditedSource], SourceAudit]:
    raw = cast(dict[str, Any], config["mathlib_docstrings"])
    catalog = Path(str(raw["catalog_path"]))
    manifest = Path(str(raw["manifest_path"]))
    reference_catalog = Path(str(raw["reference_catalog_path"]))
    _require_hash(catalog, raw["catalog_sha256"], "Mathlib docstring catalog")
    _require_hash(manifest, raw["manifest_sha256"], "Mathlib docstring manifest")
    _require_hash(reference_catalog, raw["reference_catalog_sha256"], "Mathlib reference catalog")
    _require_hash(Path(str(raw["license_path"])), raw["license_sha256"], "Mathlib license")
    context_raw = cast(dict[str, Any], config["default_context"])
    context_path = repo_root / str(context_raw["path"])
    context_hash = _require_hash(context_path, context_raw["sha256"], "default Mathlib context")
    compiled, source_context = compile_context_from_source(
        source_context_path=context_path,
        helper_path=helper_path,
        pins=pins,
    )
    context = _context_record(
        import_header=compiled.import_header,
        open_context=compiled.open_context,
        scoped_context=compiled.scoped_context,
        options=compiled.options,
        source_context_path=context_path,
        source_context_sha256=context_hash,
        pins=pins,
        helper_path=helper_path,
        configured_source_context_id=str(source_context["context_id"]),
    )
    references = {str(row["theorem_id"]): row for row in _read_jsonl(reference_catalog)}
    counts: Counter[str] = Counter()
    results: list[AuditedSource] = []
    verified_files: dict[Path, str] = {}
    audited = 0
    for row in _read_jsonl(catalog):
        audited += 1
        theorem_id = str(row.get("theorem_id", ""))
        reference = references.get(theorem_id)
        if reference is None:
            counts["missing_frozen_reference"] += 1
            continue
        doc = cast(dict[str, Any], row.get("docstring", {}))
        nl = _standalone_nl(str(doc.get("normalized_nl", "")), mathlib_docstring=True)
        if nl is None:
            counts["nonstandalone_docstring"] += 1
            continue
        screens = cast(dict[str, Any], row.get("registry_screens", {}))
        provenance_raw = cast(dict[str, Any], row.get("source_provenance", {}))
        if not (
            screens.get("all_three_screens_executed") is True
            and screens.get("all_three_screens_clear") is True
            and doc.get("immediately_attached_to_declaration_command") is True
            and provenance_raw.get("declaration_name_matches_frozen_record") is True
            and provenance_raw.get("theorem_header_matches_frozen_record") is True
        ):
            counts["catalog_integrity_screen"] += 1
            continue
        proposition = str(reference.get("signature_pp", "")).strip()
        headless = str(reference.get("headless", "")).strip()
        if not proposition or not headless or "[anonymous]" in proposition or "⋯" in proposition:
            counts["missing_or_placeholder_reference"] += 1
            continue
        source_file = str(provenance_raw.get("source_file", ""))
        source_path = Path(str(raw["mathlib_project_path"])) / source_file
        expected_source_hash = str(provenance_raw.get("source_file_sha256", ""))
        observed = verified_files.get(source_path)
        if observed is None:
            observed = _require_hash(source_path, expected_source_hash, "Mathlib theorem source")
            verified_files[source_path] = observed
        elif observed != expected_source_hash:
            counts["source_file_hash_conflict"] += 1
            continue
        raw_declaration = str(reference.get("raw_proof_stripped", ""))
        declarations = list(_DECLARATION_HEAD.finditer(mask_lean_source(raw_declaration)))
        declaration_name = declarations[-1].group(1) if declarations else None
        top_domain = source_file.split("/")[1] if source_file.count("/") >= 2 else "Other"
        source_url = (
            "https://github.com/leanprover-community/mathlib4/blob/"
            f"{raw['mathlib_revision']}/{source_file}"
        )
        source = _record(
            nl=nl,
            theorem_id=theorem_id,
            declaration_name=declaration_name,
            proposition=proposition,
            context=context,
            provenance=SourceProvenance(
                source_family="algebra" if top_domain == "Algebra" else "cross_domain",
                source_url=source_url,
                source_revision=str(raw["mathlib_revision"]),
                source_path=source_file,
                source_file_sha256=expected_source_hash,
                manifest_path=str(manifest),
                manifest_sha256=str(raw["manifest_sha256"]),
                source_recipe_sha256=config_hash,
                license_card_value=str(raw["license"]),
                redistribution_note="public Mathlib source; private-first SFT2B pilot input",
                nl_extraction_rule=str(raw["standalone_rule"]),
                trusted_reference_basis=(
                    "adjacent human-authored docstring with declaration/header match and frozen "
                    "elaborated ConstantInfo.type representation"
                ),
            ),
        )
        results.append(
            AuditedSource(
                source_class="library_docstring",
                record=source,
                headless=collapse_lean_whitespace(headless),
                near_dup_hash=signature_near_dup_hash(headless),
                problem_identity=f"mathlib::{theorem_id}",
                selection_group=top_domain,
                selection_hash=_selection_hash(seed, "library_docstring", theorem_id),
                complexity_score=len(proposition) + len(nl),
            )
        )
    if audited != int(raw["catalog_rows"]):
        raise SourceFreezeError(f"Mathlib catalog row count drifted: {audited}")
    cross_catalog = Path(str(raw["cross_domain_catalog_path"]))
    cross_manifest = Path(str(raw["cross_domain_manifest_path"]))
    _require_hash(
        cross_catalog,
        raw["cross_domain_catalog_sha256"],
        "cross-domain Mathlib docstring catalog",
    )
    _require_hash(
        cross_manifest,
        raw["cross_domain_manifest_sha256"],
        "cross-domain Mathlib docstring manifest",
    )
    cross_audited = 0
    for row in _read_jsonl(cross_catalog):
        cross_audited += 1
        doc = cast(dict[str, Any], row.get("docstring", {}))
        nl = _standalone_nl(str(doc.get("normalized_nl", "")), mathlib_docstring=True)
        if nl is None:
            counts["cross_domain_nonstandalone_docstring"] += 1
            continue
        screens = cast(dict[str, Any], row.get("registry_screens", {}))
        provenance_raw = cast(dict[str, Any], row.get("source_provenance", {}))
        theorem_raw = cast(dict[str, Any], row.get("theorem", {}))
        reference = cast(dict[str, Any], row.get("representation", {}))
        theorem_id = str(reference.get("theorem_id") or theorem_raw.get("theorem_id") or "")
        if not (
            screens.get("all_three_screens_executed") is True
            and screens.get("all_three_screens_clear") is True
            and doc.get("immediately_attached_to_declaration_command") is True
            and provenance_raw.get("declaration_name_matches_frozen_record") is True
            and provenance_raw.get("theorem_header_matches_frozen_record") is True
            and theorem_raw.get("elaboration_status") == "elaborates"
            and theorem_raw.get("is_proposition") is True
        ):
            counts["cross_domain_catalog_integrity_screen"] += 1
            continue
        proposition = str(reference.get("signature_pp", "")).strip()
        headless = str(reference.get("headless", "")).strip()
        if not proposition or not headless or "[anonymous]" in proposition or "⋯" in proposition:
            counts["cross_domain_missing_or_placeholder_reference"] += 1
            continue
        source_file = str(provenance_raw.get("source_file", ""))
        source_path = Path(str(raw["mathlib_project_path"])) / source_file
        expected_source_hash = str(provenance_raw.get("source_file_sha256", ""))
        observed = verified_files.get(source_path)
        if observed is None:
            observed = _require_hash(source_path, expected_source_hash, "Mathlib theorem source")
            verified_files[source_path] = observed
        elif observed != expected_source_hash:
            counts["cross_domain_source_file_hash_conflict"] += 1
            continue
        domain = str(row.get("domain_proxy", "Other"))
        declaration_name = str(theorem_raw.get("declaration_full_name", "")) or None
        source = _record(
            nl=nl,
            theorem_id=theorem_id,
            declaration_name=declaration_name,
            proposition=proposition,
            context=context,
            provenance=SourceProvenance(
                source_family="cross_domain",
                source_url=str(row.get("nl_source_link")),
                source_revision=str(raw["mathlib_revision"]),
                source_path=source_file,
                source_file_sha256=expected_source_hash,
                manifest_path=str(cross_manifest),
                manifest_sha256=str(raw["cross_domain_manifest_sha256"]),
                source_recipe_sha256=config_hash,
                license_card_value=str(raw["license"]),
                redistribution_note="public Mathlib source; private-first SFT2B pilot input",
                nl_extraction_rule=str(raw["standalone_rule"]),
                trusted_reference_basis=(
                    "cross-domain adjacent human-authored docstring with declaration/header match "
                    "and frozen elaborated proposition representation"
                ),
            ),
        )
        results.append(
            AuditedSource(
                source_class="library_docstring",
                record=source,
                headless=collapse_lean_whitespace(headless),
                near_dup_hash=signature_near_dup_hash(headless),
                problem_identity=f"mathlib::{theorem_id}",
                selection_group=domain,
                selection_hash=_selection_hash(seed, "library_docstring", theorem_id),
                complexity_score=len(proposition) + len(nl),
            )
        )
    if cross_audited != int(raw["cross_domain_catalog_rows"]):
        raise SourceFreezeError(f"cross-domain Mathlib catalog row count drifted: {cross_audited}")
    audited += cross_audited
    return results, SourceAudit(audited, len(results), counts)


def _numina_sources(
    config: Mapping[str, Any],
    *,
    config_hash: str,
    pins: RuntimePins,
    helper_path: Path,
    seed: str,
) -> tuple[list[AuditedSource], SourceAudit]:
    raw = cast(dict[str, Any], config["numina"])
    snapshot = Path(str(raw["snapshot_path"]))
    if snapshot.name != raw["revision"]:
        raise SourceFreezeError("Numina snapshot/revision mismatch")
    readme = snapshot / str(raw["readme_path"])
    parquet = snapshot / str(raw["train_path"])
    readme_hash = _require_hash(readme, raw["readme_sha256"], "Numina README")
    parquet_hash = _require_hash(parquet, raw["train_sha256"], "Numina train shard")
    table = pq.read_table(
        parquet,
        columns=["uuid", "data_source", "question", "valid", "proof_repair", "lean_code"],
    )
    if table.num_rows != int(raw["train_rows"]):
        raise SourceFreezeError("Numina train row count drifted")
    counts: Counter[str] = Counter()
    results: list[AuditedSource] = []
    for row in table.to_pylist():
        if row["valid"] is not True:
            counts["valid_not_true"] += 1
            continue
        if row["proof_repair"] is not False:
            counts["proof_repair_not_false"] += 1
            continue
        question = str(row["question"])
        lean_code = str(row["lean_code"])
        fences = _FENCE.findall(question)
        if len(fences) != 1:
            counts["not_one_lean_fence"] += 1
            continue
        question_code = fences[0]
        try:
            question_declaration, question_name, declaration_start = _last_declaration(
                question_code
            )
            lean_declaration, lean_name, _ = _last_declaration(lean_code)
        except SourceFreezeError:
            counts["declaration_extraction_failure"] += 1
            continue
        if question_name != lean_name:
            counts["declaration_name_mismatch"] += 1
            continue
        question_headless = normalize_headless(question_declaration)
        lean_headless = normalize_headless(lean_declaration)
        if question_headless is None or lean_headless is None:
            counts["headless_failure"] += 1
            continue
        headless = collapse_lean_whitespace(question_headless)
        if headless != collapse_lean_whitespace(lean_headless):
            counts["question_reference_mismatch"] += 1
            continue
        comments = _block_comments(question_code[:declaration_start])
        if not comments:
            counts["missing_adjacent_nl_comment"] += 1
            continue
        comment_start, comment_finish, comment = comments[-1]
        if question_code[comment_finish:declaration_start].strip():
            counts["nl_comment_not_adjacent"] += 1
            continue
        nl = _standalone_nl(comment, mathlib_docstring=False)
        if nl is None:
            counts["nonstandalone_nl"] += 1
            continue
        parsed_header = _parse_header(question_code[:comment_start])
        if parsed_header is None:
            counts["unsupported_compile_context"] += 1
            continue
        import_header, opens, scoped, options = parsed_header
        if "sorry" in lean_code.casefold() or not lean_code.split("by", 1)[-1].strip():
            counts["untrusted_completed_reference"] += 1
            continue
        try:
            proposition = _closed_proposition(headless)
        except SourceFreezeError:
            counts["closed_proposition_failure"] += 1
            continue
        context = _context_record(
            import_header=import_header,
            open_context=opens,
            scoped_context=scoped,
            options=options,
            source_context_path=parquet,
            source_context_sha256=parquet_hash,
            pins=pins,
            helper_path=helper_path,
        )
        uuid = str(row["uuid"])
        data_source = str(row["data_source"])
        theorem_id = f"numina:{uuid}:{question_name}"
        source = _record(
            nl=nl,
            theorem_id=theorem_id,
            declaration_name=question_name,
            proposition=proposition,
            context=context,
            provenance=SourceProvenance(
                source_family="new_audited",
                source_url=(
                    f"https://huggingface.co/datasets/{raw['dataset_id']}/blob/"
                    f"{raw['revision']}/{raw['train_path']}"
                ),
                source_revision=str(raw["revision"]),
                source_path=str(parquet),
                source_file_sha256=parquet_hash,
                manifest_path=str(readme),
                manifest_sha256=readme_hash,
                source_recipe_sha256=config_hash,
                license_card_value=str(raw["license"]),
                redistribution_note=(
                    "source_use_v2 owner-authorized external processing; private-first; public "
                    "redistribution still requires review"
                ),
                nl_extraction_rule="unique adjacent block comment before the final theorem",
                trusted_reference_basis=(
                    "valid=true, proof_repair=false, exact question/completed-lean signature "
                    "agreement, and non-placeholder completed Lean evidence"
                ),
            ),
        )
        domain_match = re.fullmatch(r"(.+?)_[0-9]+", question_name)
        domain = domain_match.group(1) if domain_match else data_source
        results.append(
            AuditedSource(
                source_class="theorem_problem",
                record=source,
                headless=headless,
                near_dup_hash=signature_near_dup_hash(headless),
                problem_identity=f"{data_source}::{question_name}",
                selection_group=domain,
                selection_hash=_selection_hash(seed, "theorem_problem", uuid),
                complexity_score=len(proposition) + len(nl),
            )
        )
    return results, SourceAudit(table.num_rows, len(results), counts)


def _workbook_domain(nl: str) -> str:
    lowered = nl.casefold()
    rules = (
        ("geometry", ("triangle", "circle", "angle", "polygon", "geometry")),
        ("number_theory", ("integer", "prime", "divisible", "divisor", "modulo")),
        ("combinatorics", ("permutation", "combination", "arrange", "coloring", "graph")),
        ("analysis", ("continuous", "derivative", "integral", "limit", "sequence")),
        ("algebra", ("polynomial", "equation", "inequality", "function", "matrix")),
    )
    for domain, words in rules:
        if any(word in lowered for word in words):
            return domain
    return "other"


def _workbook_sources(
    config: Mapping[str, Any],
    *,
    repo_root: Path,
    config_hash: str,
    pins: RuntimePins,
    helper_path: Path,
    seed: str,
) -> tuple[list[AuditedSource], SourceAudit]:
    raw = cast(dict[str, Any], config["lean_workbook"])
    snapshot = Path(str(raw["snapshot_path"]))
    if snapshot.name != f"lean_workbook_{raw['revision']}":
        raise SourceFreezeError("Lean-Workbook snapshot/revision mismatch")
    readme = snapshot / str(raw["readme_path"])
    parquet = snapshot / str(raw["parquet_path"])
    readme_hash = _require_hash(readme, raw["readme_sha256"], "Lean-Workbook README")
    parquet_hash = _require_hash(parquet, raw["parquet_sha256"], "Lean-Workbook parquet")
    table = pq.read_table(parquet)
    if table.num_rows != int(raw["rows"]):
        raise SourceFreezeError("Lean-Workbook row count drifted")
    default_raw = cast(dict[str, Any], config["default_context"])
    context_path = repo_root / str(default_raw["path"])
    context_hash = _require_hash(context_path, default_raw["sha256"], "default Mathlib context")
    compiled, source_context = compile_context_from_source(
        source_context_path=context_path,
        helper_path=helper_path,
        pins=pins,
    )
    context = _context_record(
        import_header=compiled.import_header,
        open_context=compiled.open_context,
        scoped_context=compiled.scoped_context,
        options=compiled.options,
        source_context_path=context_path,
        source_context_sha256=context_hash,
        pins=pins,
        helper_path=helper_path,
        configured_source_context_id=str(source_context["context_id"]),
    )
    counts: Counter[str] = Counter()
    results: list[AuditedSource] = []
    for row in table.to_pylist():
        if row.get("status") != "proved":
            counts["status_not_proved"] += 1
            continue
        if str(row.get("state_after", "")).strip() != "no goals":
            counts["state_after_not_no_goals"] += 1
            continue
        if not str(row.get("tactic", "")).strip():
            counts["empty_tactic_evidence"] += 1
            continue
        nl = _standalone_nl(str(row.get("natural_language_statement", "")), mathlib_docstring=False)
        if nl is None:
            counts["nonstandalone_nl"] += 1
            continue
        formal = str(row.get("formal_statement", ""))
        try:
            declaration, declaration_name, _ = _last_declaration(formal)
        except SourceFreezeError:
            counts["declaration_extraction_failure"] += 1
            continue
        row_id = str(row.get("id", ""))
        if declaration_name != row_id:
            counts["declaration_id_mismatch"] += 1
            continue
        headless_raw = normalize_headless(declaration)
        if headless_raw is None:
            counts["headless_failure"] += 1
            continue
        headless = collapse_lean_whitespace(headless_raw)
        try:
            proposition = _closed_proposition(headless)
        except SourceFreezeError:
            counts["closed_proposition_failure"] += 1
            continue
        source = _record(
            nl=nl,
            theorem_id=f"lean_workbook:{row_id}",
            declaration_name=declaration_name,
            proposition=proposition,
            context=context,
            provenance=SourceProvenance(
                source_family="new_audited",
                source_url=(
                    f"https://huggingface.co/datasets/{raw['dataset_id']}/blob/"
                    f"{raw['revision']}/{raw['parquet_path']}"
                ),
                source_revision=str(raw["revision"]),
                source_path=str(parquet),
                source_file_sha256=parquet_hash,
                manifest_path=str(readme),
                manifest_sha256=readme_hash,
                source_recipe_sha256=config_hash,
                license_card_value=str(raw["license"]),
                redistribution_note=(
                    "Apache-2.0 synthetic weak supervision; ReForm-training overlap=true; "
                    "private-first and never held-out-generator/source-independent evidence"
                ),
                nl_extraction_rule="pinned natural_language_statement field",
                trusted_reference_basis=(
                    "pinned status=proved row with nonempty tactic and state_after exactly no goals"
                ),
            ),
        )
        domain = _workbook_domain(nl)
        results.append(
            AuditedSource(
                source_class="broader_public_synthetic",
                record=source,
                headless=headless,
                near_dup_hash=signature_near_dup_hash(headless),
                problem_identity=f"lean_workbook::{row_id}",
                selection_group=domain,
                selection_hash=_selection_hash(seed, "lean_workbook", row_id),
                complexity_score=len(proposition) + len(nl) + headless.count("(") * 8,
            )
        )
    return results, SourceAudit(table.num_rows, len(results), counts)


def _screen_contamination(
    candidates: Sequence[AuditedSource],
    *,
    golden_exact: frozenset[str],
    golden_blocklist: GoldenBlocklist,
    existing_proposition_hashes: frozenset[str],
    existing_nl: frozenset[str],
) -> tuple[list[AuditedSource], Counter[str]]:
    clear: list[AuditedSource] = []
    counts: Counter[str] = Counter()
    local_prop: set[str] = set()
    local_nl: set[str] = set()
    local_near: set[str] = set()
    for item in candidates:
        prop_hash = item.record.reference_proposition_sha256
        nl_key = _nl_key(item.record.nl_statement)
        if item.headless in golden_exact:
            counts["golden_exact"] += 1
        elif item.near_dup_hash in golden_blocklist.near_dup_hashes:
            counts["golden_near"] += 1
        elif golden_blocklist.problem_is_blocked(item.problem_identity):
            counts["golden_problem_identity"] += 1
        elif prop_hash in existing_proposition_hashes or nl_key in existing_nl:
            counts["existing_301_overlap"] += 1
        elif prop_hash in local_prop:
            counts["duplicate_reference_proposition"] += 1
        elif nl_key in local_nl:
            counts["duplicate_nl"] += 1
        elif item.near_dup_hash in local_near:
            counts["duplicate_signature_near_hash"] += 1
        else:
            clear.append(item)
            local_prop.add(prop_hash)
            local_nl.add(nl_key)
            local_near.add(item.near_dup_hash)
    return clear, counts


def _round_robin_select(
    candidates: Sequence[AuditedSource],
    count: int,
    *,
    seen_prop: set[str],
    seen_nl: set[str],
    seen_near: set[str],
) -> list[AuditedSource]:
    groups: dict[str, list[AuditedSource]] = defaultdict(list)
    for item in candidates:
        groups[item.selection_group].append(item)
    for values in groups.values():
        values.sort(key=lambda item: (item.selection_hash, item.record.source_id))
    selected: list[AuditedSource] = []
    group_names = sorted(groups)
    offsets = dict.fromkeys(group_names, 0)
    while len(selected) < count:
        progressed = False
        for name in group_names:
            values = groups[name]
            while offsets[name] < len(values):
                item = values[offsets[name]]
                offsets[name] += 1
                prop = item.record.reference_proposition_sha256
                nl = _nl_key(item.record.nl_statement)
                near = item.near_dup_hash
                if prop in seen_prop or nl in seen_nl or near in seen_near:
                    continue
                selected.append(item)
                seen_prop.add(prop)
                seen_nl.add(nl)
                seen_near.add(near)
                progressed = True
                break
            if len(selected) == count:
                break
        if not progressed:
            raise SourceFreezeError(f"source pool exhausted at {len(selected)}/{count}")
    return selected


def _ranked_select(
    candidates: Sequence[AuditedSource],
    count: int,
    *,
    seen_prop: set[str],
    seen_nl: set[str],
    seen_near: set[str],
) -> list[AuditedSource]:
    selected: list[AuditedSource] = []
    for item in sorted(
        candidates,
        key=lambda value: (-value.complexity_score, value.selection_hash, value.record.source_id),
    ):
        prop = item.record.reference_proposition_sha256
        nl = _nl_key(item.record.nl_statement)
        near = item.near_dup_hash
        if prop in seen_prop or nl in seen_nl or near in seen_near:
            continue
        selected.append(item)
        seen_prop.add(prop)
        seen_nl.add(nl)
        seen_near.add(near)
        if len(selected) == count:
            return selected
    raise SourceFreezeError(f"ranked source pool exhausted at {len(selected)}/{count}")


def _tokenizer(config: Mapping[str, Any], *, snapshot_override: Path | None = None) -> Any:
    raw = cast(dict[str, Any], config["tokenizer"])
    snapshot = snapshot_override or Path(str(raw["snapshot_path"]))
    if not snapshot.is_dir():
        raise SourceFreezeError(f"missing pinned tokenizer snapshot: {snapshot}")
    files = cast(dict[str, str], raw["files"])
    actual = {item.name for item in snapshot.iterdir() if item.is_file()}
    if snapshot_override is None and actual != set(files):
        raise SourceFreezeError(
            f"tokenizer file set mismatch: expected {sorted(files)}, observed {sorted(actual)}"
        )
    if snapshot_override is not None and not set(files).issubset(actual):
        raise SourceFreezeError(
            f"model snapshot lacks pinned tokenizer files: {sorted(set(files).difference(actual))}"
        )
    for name, expected in files.items():
        _require_hash(snapshot / name, expected, f"tokenizer asset {name}")
    from transformers import AutoTokenizer

    return AutoTokenizer.from_pretrained(  # type: ignore[no-untyped-call]
        snapshot,
        local_files_only=True,
        trust_remote_code=False,
    )


def _render_prompt(template: str, source: SourceRecord) -> str:
    if template.count("{{NL}}") != 1:
        raise SourceFreezeError("prompt must contain exactly one NL placeholder")
    prompt = template.replace("{{NL}}", source.nl_statement)
    if "{{NL}}" in prompt or source.reference_proposition in prompt:
        raise SourceFreezeError("rendered prompt is incomplete or leaks the reference")
    return prompt


def _git_head(repo_root: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def build_bundle(repo_root: Path, *, config_path: Path, output_dir: Path) -> FreezeResult:
    """Build exactly four deterministic pilot-input files."""

    config = _object(config_path)
    if config.get("schema_version") != "sft2b_reform_matched_500_sources_v1":
        raise SourceFreezeError("unsupported matched-500 source config")
    if config.get("source_mix") != EXPECTED_MIX or config.get("expected_rows") != 500:
        raise SourceFreezeError("matched-500 mix drifted")
    config_hash = hash_file(config_path)
    for key in ("runtime_config", "source_use_policy"):
        raw = cast(dict[str, Any], config[key])
        _require_hash(repo_root / str(raw["path"]), raw["sha256"], key)
    policy_raw = cast(dict[str, Any], config["source_use_policy"])
    policy = yaml.safe_load((repo_root / str(policy_raw["path"])).read_text(encoding="utf-8"))
    if not isinstance(policy, dict) or not (
        policy.get("policy_version") == "source_use_v2"
        and policy.get("external_model_processing") is True
        and cast(dict[str, Any], policy.get("scope", {})).get("namespace") == "formalmathatepfl/*"
    ):
        raise SourceFreezeError("source_use_v2 no longer authorizes the Numina source")
    contamination = cast(dict[str, Any], config["contamination"])
    for path_key, hash_key, label in (
        ("golden_blocklist_path", "golden_blocklist_sha256", "golden blocklist"),
        ("canonical_golden_path", "canonical_golden_sha256", "canonical golden pairs"),
        ("benchmark_registry_path", "benchmark_registry_sha256", "benchmark registry"),
        ("benchmark_denylist_path", "benchmark_denylist_sha256", "benchmark denylist"),
    ):
        value = Path(str(contamination[path_key]))
        path = value if value.is_absolute() else repo_root / value
        _require_hash(path, contamination[hash_key], label)
    if (
        contamination.get("exclude_consistency_check") is not True
        or contamination.get("exclude_shadowbench") is not True
    ):
        raise SourceFreezeError("evaluation-only source exclusions drifted")
    helper_path = repo_root / "src/leanfaith/sft2b/lean_helper.lean"
    pins = verify_runtime_pins(repo_root, helper_path=helper_path)
    repr_raw = cast(dict[str, Any], config["repr"])
    expected_repr = {
        "freeze_commit": pins.repr_freeze_commit,
        "spec_sha256": pins.repr_spec_hash,
        "implementation_set_sha256": pins.repr_implementation_set_hash,
        "api_sha256": pins.repr_api_hash,
    }
    if repr_raw != expected_repr:
        raise SourceFreezeError("REPR pins in source config drifted")
    existing_raw = cast(dict[str, Any], config["existing_301"])
    recipe_path = repo_root / str(existing_raw["recipe_path"])
    _require_hash(recipe_path, existing_raw["recipe_sha256"], "existing-301 recipe")
    existing, existing_receipt = load_existing_301(
        repo_root,
        recipe_path=recipe_path,
        helper_path=helper_path,
        pins=pins,
    )
    if not (
        len(existing) == int(existing_raw["expected_candidates"])
        and len({item.source.source_id for item in existing})
        == int(existing_raw["expected_unique_references"])
        and existing_receipt.consumed_bundle_sha256 == existing_raw["consumed_bundle_sha256"]
    ):
        raise SourceFreezeError("existing-301 receipt drifted")
    existing_prop = frozenset(item.source.reference_proposition_sha256 for item in existing)
    existing_nl = frozenset(_nl_key(item.source.nl_statement) for item in existing)
    seed = str(config["selection_seed"])
    mathlib, mathlib_audit = _mathlib_sources(
        config,
        repo_root=repo_root,
        config_hash=config_hash,
        pins=pins,
        helper_path=helper_path,
        seed=seed,
    )
    numina, numina_audit = _numina_sources(
        config,
        config_hash=config_hash,
        pins=pins,
        helper_path=helper_path,
        seed=seed,
    )
    workbook, workbook_audit = _workbook_sources(
        config,
        repo_root=repo_root,
        config_hash=config_hash,
        pins=pins,
        helper_path=helper_path,
        seed=seed,
    )
    if (
        min(
            mathlib_audit.audited_rows,
            numina_audit.audited_rows,
            workbook_audit.audited_rows,
        )
        < 100
    ):
        raise SourceFreezeError("each proposed source class requires at least 100 audited rows")
    blocklist_path = repo_root / str(contamination["golden_blocklist_path"])
    canonical_path = Path(str(contamination["canonical_golden_path"]))
    blocklist = GoldenBlocklist.load(blocklist_path)
    golden_exact = _golden_exact(canonical_path)
    class_clear: dict[str, list[AuditedSource]] = {}
    contamination_counts: dict[str, dict[str, int]] = {}
    for name, candidates in (
        ("library_docstring", mathlib),
        ("theorem_problem", numina),
        ("lean_workbook", workbook),
    ):
        clear, counts = _screen_contamination(
            candidates,
            golden_exact=golden_exact,
            golden_blocklist=blocklist,
            existing_proposition_hashes=existing_prop,
            existing_nl=existing_nl,
        )
        class_clear[name] = clear
        contamination_counts[name] = dict(sorted(counts.items()))
    seen_prop: set[str] = set()
    seen_nl: set[str] = set()
    seen_near: set[str] = set()
    selected_mathlib = _round_robin_select(
        class_clear["library_docstring"],
        EXPECTED_MIX["library_docstring"],
        seen_prop=seen_prop,
        seen_nl=seen_nl,
        seen_near=seen_near,
    )
    selected_numina = _round_robin_select(
        class_clear["theorem_problem"],
        EXPECTED_MIX["theorem_problem"],
        seen_prop=seen_prop,
        seen_nl=seen_nl,
        seen_near=seen_near,
    )
    selected_broader = _round_robin_select(
        class_clear["lean_workbook"],
        EXPECTED_MIX["broader_public_synthetic"],
        seen_prop=seen_prop,
        seen_nl=seen_nl,
        seen_near=seen_near,
    )
    selected_specialist = _ranked_select(
        class_clear["lean_workbook"],
        EXPECTED_MIX["specialist_high_difficulty"],
        seen_prop=seen_prop,
        seen_nl=seen_nl,
        seen_near=seen_near,
    )
    selected_with_classes = [
        *(("library_docstring", item) for item in selected_mathlib),
        *(("theorem_problem", item) for item in selected_numina),
        *(("broader_public_synthetic", item) for item in selected_broader),
        *(("specialist_high_difficulty", item) for item in selected_specialist),
    ]
    rows = tuple(item.record for _, item in selected_with_classes)
    if len(rows) != 500 or len({row.source_id for row in rows}) != 500:
        raise SourceFreezeError("selected source IDs are not exactly 500 unique rows")
    mix = Counter(name for name, _ in selected_with_classes)
    if dict(mix) != EXPECTED_MIX:
        raise SourceFreezeError(f"selected source mix drifted: {dict(mix)}")
    placement_raw = cast(dict[str, Any], config["placement"])
    placement_path = repo_root / str(placement_raw["path"])
    placement_hash = _require_hash(placement_path, placement_raw["sha256"], "placement config")
    placement = _object(placement_path)
    if placement.get("candidate_slots") != placement_raw["slots"]:
        raise SourceFreezeError("candidate slots/seeds drifted from placement config")
    decoding = cast(dict[str, Any], placement["decoding"])
    if decoding.get("max_new_tokens") != placement_raw["max_new_tokens"]:
        raise SourceFreezeError("max_new_tokens drifted from placement config")
    prompt_raw = cast(dict[str, Any], config["prompt"])
    prompt_path = repo_root / str(prompt_raw["path"])
    prompt_hash = _require_hash(prompt_path, prompt_raw["sha256"], "ReForm prompt")
    prompt_template = prompt_path.read_text(encoding="utf-8")
    tokenizer = _tokenizer(config)
    token_rows: list[dict[str, object]] = []
    maximum = 0
    for source in rows:
        prompt = _render_prompt(prompt_template, source)
        encoded = tokenizer(prompt, return_tensors="pt")
        token_count = int(encoded["input_ids"].shape[1])
        maximum = max(maximum, token_count)
        token_rows.append(
            {
                "source_id": source.source_id,
                "prompt_sha256": sha256_hex(prompt.encode("utf-8")),
                "prompt_tokens": token_count,
            }
        )
    required_max_model_len = maximum + int(placement_raw["max_new_tokens"])
    output_dir.mkdir(parents=True, exist_ok=True)
    unexpected = {item.name for item in output_dir.iterdir() if item.is_file()} - set(OUTPUT_NAMES)
    if unexpected:
        raise SourceFreezeError(f"output directory contains unexpected files: {sorted(unexpected)}")
    sources_path = output_dir / "sources.jsonl"
    with sources_path.open("wb") as handle:
        for source in rows:
            handle.write(_canonical_line(source.model_dump(mode="json")))
    token_payload = {
        "schema_version": PROMPT_COUNTS_SCHEMA_VERSION,
        "source_count": len(rows),
        "model_id": placement_raw["model_id"],
        "model_revision": placement_raw["model_revision"],
        "prompt_path": prompt_raw["path"],
        "prompt_sha256": prompt_hash,
        "tokenizer_model_id": cast(dict[str, Any], config["tokenizer"])["model_id"],
        "tokenizer_revision": cast(dict[str, Any], config["tokenizer"])["revision"],
        "tokenizer_sha256": cast(dict[str, Any], config["tokenizer"])["primary_sha256"],
        "maximum_prompt_tokens": maximum,
        "max_new_tokens": placement_raw["max_new_tokens"],
        "required_max_model_len": required_max_model_len,
        "rows": token_rows,
    }
    token_path = output_dir / "prompt_token_counts.json"
    token_path.write_bytes(_canonical_line(token_payload))
    source_mix_details = {
        "requested": EXPECTED_MIX,
        "selected": dict(sorted(mix.items())),
        "selected_domain_counts": {
            name: dict(sorted(Counter(item.selection_group for item in values).items()))
            for name, values in (
                ("library_docstring", selected_mathlib),
                ("theorem_problem", selected_numina),
                ("broader_public_synthetic", selected_broader),
                ("specialist_high_difficulty", selected_specialist),
            )
        },
    }
    schema_file = repo_root / "src/leanfaith/sft2b/schemas.py"
    schema_hash = hash_file(schema_file)
    source_schema_hash = hash_canonical(SourceRecord.model_json_schema())
    source_hash = hash_file(sources_path)
    token_hash = hash_file(token_path)
    manifest_payload = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "bundle_schema_version": SCHEMA_VERSION,
        "builder_git_commit": _git_head(repo_root),
        "source_config_path": str(config_path.relative_to(repo_root)),
        "source_config_sha256": config_hash,
        "source_count": len(rows),
        "source_mix": source_mix_details,
        "selection_seed": seed,
        "selection_rules": {
            "global": [
                "strict SourceRecord validation and stable-ID replay",
                "standalone NL and frozen successful/trusted reference evidence",
                "golden exact, signature-near, and problem-identity exclusion",
                "existing-301 proposition/NL exclusion",
                "global NL, proposition, and signature-near deduplication",
            ],
            "mathlib": cast(dict[str, Any], config["mathlib_docstrings"])["selection_rule"],
            "numina": cast(dict[str, Any], config["numina"])["selection_rule"],
            "lean_workbook_broader": cast(dict[str, Any], config["lean_workbook"])[
                "broader_selection_rule"
            ],
            "lean_workbook_specialist": cast(dict[str, Any], config["lean_workbook"])[
                "specialist_selection_rule"
            ],
        },
        "source_audits": {
            "library_docstring": mathlib_audit.to_dict(),
            "theorem_problem": numina_audit.to_dict(),
            "lean_workbook_for_broader_and_specialist": workbook_audit.to_dict(),
        },
        "contamination": {
            "golden_blocklist_sha256": contamination["golden_blocklist_sha256"],
            "canonical_golden_sha256": contamination["canonical_golden_sha256"],
            "canonical_exact_signature_count": len(golden_exact),
            "class_exclusion_counts": contamination_counts,
            "selected_exact_hits": 0,
            "selected_near_hits": 0,
            "selected_problem_identity_hits": 0,
            "selected_existing_301_hits": 0,
            "selected_internal_duplicates": 0,
            "consistency_check": "excluded_evaluation_only",
            "shadowbench": "excluded_reference_free_test_only_126_rows",
        },
        "source_catalogs": {
            "mathlib_docstrings": config["mathlib_docstrings"],
            "numina": config["numina"],
            "lean_workbook": config["lean_workbook"],
        },
        "licenses_and_policies": {
            "source_use_policy": config["source_use_policy"],
            "mathlib": "Apache-2.0",
            "numina": "not_declared_in_pinned_readme; private-first only",
            "lean_workbook": (
                "Apache-2.0; ReForm-training overlap; not held-out-generator or "
                "source-independent evidence"
            ),
        },
        "existing_301_replay": {
            **existing_raw,
            "observed_candidate_count": len(existing),
            "observed_unique_reference_count": len({item.source.source_id for item in existing}),
            "all_semantic_labels_unknown": existing_receipt.all_unknown,
        },
        "schemas": {
            "source_record_schema_version": "sft2b_source_v1",
            "source_record_json_schema_sha256": source_schema_hash,
            "schemas_py_sha256": schema_hash,
            "prompt_token_counts_schema_version": PROMPT_COUNTS_SCHEMA_VERSION,
        },
        "prompt": {
            **prompt_raw,
            "observed_sha256": prompt_hash,
        },
        "tokenizer": config["tokenizer"],
        "placement": {
            "path": placement_raw["path"],
            "sha256": placement_hash,
            "model_id": placement_raw["model_id"],
            "model_revision": placement_raw["model_revision"],
            "candidate_slots": placement_raw["slots"],
            "decoding": decoding,
            "required_max_model_len": required_max_model_len,
        },
        "repr": {
            **repr_raw,
            "verified_runtime_pins": pins.to_dict(),
        },
        "prompt_tokens": {
            "maximum_prompt_tokens": maximum,
            "max_new_tokens": placement_raw["max_new_tokens"],
            "required_max_model_len": required_max_model_len,
        },
        "data_files": {
            "sources.jsonl": {"rows": len(rows), "sha256": source_hash},
            "prompt_token_counts.json": {"rows": len(token_rows), "sha256": token_hash},
        },
    }
    manifest_path = output_dir / "source_manifest.json"
    manifest_path.write_bytes(_canonical_line(manifest_payload))
    checksums = {
        "prompt_token_counts.json": token_hash,
        "source_manifest.json": hash_file(manifest_path),
        "sources.jsonl": source_hash,
    }
    checksum_path = output_dir / "SHA256SUMS"
    checksum_path.write_text(
        "".join(f"{digest}  {name}\n" for name, digest in sorted(checksums.items())),
        encoding="utf-8",
    )
    file_hashes = {name: hash_file(output_dir / name) for name in OUTPUT_NAMES}
    result = FreezeResult(
        output_dir=output_dir,
        rows=rows,
        source_mix=dict(sorted(mix.items())),
        maximum_prompt_tokens=maximum,
        required_max_model_len=required_max_model_len,
        file_sha256=file_hashes,
    )
    verify_bundle(repo_root, config_path=config_path, bundle_dir=output_dir)
    return result


def verify_bundle(
    repo_root: Path,
    *,
    config_path: Path,
    bundle_dir: Path,
    tokenizer_snapshot_path: Path | None = None,
) -> FreezeResult:
    """Verify a bundle from only its four bytes plus pinned repo/tokenizer files."""

    names = {item.name for item in bundle_dir.iterdir() if item.is_file()}
    if names != set(OUTPUT_NAMES):
        raise SourceFreezeError(
            f"bundle file set mismatch: expected {sorted(OUTPUT_NAMES)}, observed {sorted(names)}"
        )
    checksum_lines = (bundle_dir / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    expected_checksums: dict[str, str] = {}
    for line in checksum_lines:
        match = re.fullmatch(r"([0-9a-f]{64})  (\S+)", line)
        if match is None:
            raise SourceFreezeError("malformed SHA256SUMS line")
        expected_checksums[match.group(2)] = match.group(1)
    if set(expected_checksums) != {
        "sources.jsonl",
        "prompt_token_counts.json",
        "source_manifest.json",
    }:
        raise SourceFreezeError("SHA256SUMS coverage drifted")
    for name, digest in expected_checksums.items():
        if hash_file(bundle_dir / name) != digest:
            raise SourceFreezeError(f"bundle checksum mismatch: {name}")
    rows = tuple(
        SourceRecord.model_validate(row) for row in _read_jsonl(bundle_dir / "sources.jsonl")
    )
    if len(rows) != 500 or len({row.source_id for row in rows}) != 500:
        raise SourceFreezeError("fresh bundle does not contain 500 unique SourceRecords")
    token_payload = _object(bundle_dir / "prompt_token_counts.json")
    manifest = _object(bundle_dir / "source_manifest.json")
    if token_payload.get("schema_version") != PROMPT_COUNTS_SCHEMA_VERSION:
        raise SourceFreezeError("prompt-token schema version drifted")
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise SourceFreezeError("source manifest schema version drifted")
    token_rows = token_payload.get("rows")
    if not isinstance(token_rows, list) or len(token_rows) != 500:
        raise SourceFreezeError("prompt-token rows are not exactly 500")
    if [row.source_id for row in rows] != [item.get("source_id") for item in token_rows]:
        raise SourceFreezeError("source/prompt-token ordering drifted")
    config = _object(config_path)
    prompt_raw = cast(dict[str, Any], config["prompt"])
    prompt_path = repo_root / str(prompt_raw["path"])
    _require_hash(prompt_path, prompt_raw["sha256"], "ReForm prompt")
    template = prompt_path.read_text(encoding="utf-8")
    tokenizer = _tokenizer(config, snapshot_override=tokenizer_snapshot_path)
    maximum = 0
    for source, token_row_raw in zip(rows, token_rows, strict=True):
        if not isinstance(token_row_raw, dict):
            raise SourceFreezeError("prompt-token row is not an object")
        token_row = cast(dict[str, Any], token_row_raw)
        prompt = _render_prompt(template, source)
        observed_hash = sha256_hex(prompt.encode("utf-8"))
        observed_tokens = int(tokenizer(prompt, return_tensors="pt")["input_ids"].shape[1])
        if (
            token_row.get("prompt_sha256") != observed_hash
            or token_row.get("prompt_tokens") != observed_tokens
        ):
            raise SourceFreezeError(f"prompt-token replay mismatch: {source.source_id}")
        maximum = max(maximum, observed_tokens)
    required = maximum + int(cast(dict[str, Any], config["placement"])["max_new_tokens"])
    if (
        token_payload.get("maximum_prompt_tokens") != maximum
        or token_payload.get("required_max_model_len") != required
    ):
        raise SourceFreezeError("maximum prompt length replay mismatch")
    selected_mix = cast(dict[str, Any], cast(dict[str, Any], manifest["source_mix"])["selected"])
    if selected_mix != EXPECTED_MIX:
        raise SourceFreezeError("manifest source mix drifted")
    data_files = cast(dict[str, Any], manifest["data_files"])
    if cast(dict[str, Any], data_files["sources.jsonl"])["sha256"] != hash_file(
        bundle_dir / "sources.jsonl"
    ) or cast(dict[str, Any], data_files["prompt_token_counts.json"])["sha256"] != hash_file(
        bundle_dir / "prompt_token_counts.json"
    ):
        raise SourceFreezeError("manifest data-file hashes drifted")
    return FreezeResult(
        output_dir=bundle_dir,
        rows=rows,
        source_mix=selected_mix,
        maximum_prompt_tokens=maximum,
        required_max_model_len=required,
        file_sha256={name: hash_file(bundle_dir / name) for name in OUTPUT_NAMES},
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("configs/sft2b/reform_matched_500_sources_v1.json"),
    )
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--verify-dir", type=Path)
    return parser


def main() -> None:
    args = _parser().parse_args()
    repo_root = Path.cwd()
    config_path = args.config if args.config.is_absolute() else repo_root / args.config
    if (args.output_dir is None) == (args.verify_dir is None):
        raise SystemExit("provide exactly one of --output-dir or --verify-dir")
    if args.output_dir is not None:
        result = build_bundle(repo_root, config_path=config_path, output_dir=args.output_dir)
    else:
        result = verify_bundle(repo_root, config_path=config_path, bundle_dir=args.verify_dir)
    print(
        json.dumps(
            {
                "output_dir": str(result.output_dir),
                "rows": len(result.rows),
                "source_mix": result.source_mix,
                "maximum_prompt_tokens": result.maximum_prompt_tokens,
                "required_max_model_len": result.required_max_model_len,
                "file_sha256": result.file_sha256,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
