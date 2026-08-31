"""Freeze every quality-qualified SFT2B source without invoking Lean or a model.

The builder joins already-elaborated closed propositions to standalone natural
language, applies the frozen benchmark and existing-data screens, globally
deduplicates the surviving pool, renders the pinned ReForm prompt, and writes
a deterministic private-first release bundle.
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

from leanfaith.collect2.postprocess import GoldenBlocklist
from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.representations.goal_v1 import CompileContext
from leanfaith.representations.views import (
    collapse_lean_whitespace,
    normalize_headless,
    signature_near_dup_hash,
)
from leanfaith.sft2b.pilot_source_freeze import (
    AuditedSource,
    SourceAudit,
    SourceFreezeError,
    _closed_proposition,
    _golden_exact,
    _helper_body,
    _last_declaration,
    _nl_key,
    _numina_sources,
    _object,
    _parse_header,
    _read_jsonl,
    _record,
    _require_hash,
    _selection_hash,
    _standalone_nl,
    _tokenizer,
    _workbook_sources,
)
from leanfaith.sft2b.pins import RuntimePins, verify_runtime_pins
from leanfaith.sft2b.reuse import load_existing_301
from leanfaith.sft2b.schemas import CompileContextRecord, SourceProvenance, SourceRecord

SCHEMA_VERSION = "sft2b_reform_diverse_full_bundle_v1"
MANIFEST_SCHEMA_VERSION = "sft2b_diverse_full_source_manifest_v1"
PROMPT_COUNTS_SCHEMA_VERSION = "sft2b_prompt_token_counts_v1"
AUDIT_SCHEMA_VERSION = "sft2b_source_selection_audit_v1"
MATCHED_VIEW_SCHEMA_VERSION = "sft2b_matched_50k_view_v1"
OUTPUT_NAMES = (
    "sources.jsonl",
    "prompt_token_counts.json",
    "source_audit.jsonl",
    "matched_50000_source_ids.json",
    "source_manifest.json",
    "SHA256SUMS",
)

_WORDS = re.compile(r"[A-Za-z]{2,}")
_ATTRIBUTES_ONLY = re.compile(r"(?:\s|@\[(?:[^\[\]]|\[[^\]]*\])*\])*\Z", re.DOTALL)
_GENERIC_LIBRARY_DOC = re.compile(
    r"^(?:characteri[sz]ation theorem|the (?:dual|following|corresponding) axiom|"
    r"same logic|speciali[sz]e|reduction\s*:|an? (?:analogue|variant|version|special case)|"
    r"theorem that|lemma that)|\b(?:as above|as below|the previous|the next)\b",
    re.IGNORECASE,
)
_DENIED_SOURCE_MARKERS = (
    "proofnet",
    "proofnetverif",
    "proofnetsharp",
    "proofnet#",
    "shadowbench",
)
_PLACEHOLDERS = ("[anonymous]", "⋯", "…")


@dataclass(frozen=True, slots=True)
class QualifiedSource:
    audited: AuditedSource
    release_class: str
    trust_tier: str
    domain: str
    upstream_source: str


@dataclass(frozen=True, slots=True)
class FreezeResult:
    output_dir: Path
    rows: tuple[SourceRecord, ...]
    source_mix: dict[str, int]
    maximum_prompt_tokens: int
    required_max_model_len: int
    file_sha256: dict[str, str]


def _canonical_line(value: object) -> bytes:
    return canonical_json_bytes(value) + b"\n"


def _render_source_prompt(template: str, source: SourceRecord) -> str:
    """Apply the frozen NL-only template without substring-based false positives."""

    if template.count("{{NL}}") != 1:
        raise SourceFreezeError("prompt must contain exactly one NL placeholder")
    prompt = template.replace("{{NL}}", source.nl_statement)
    if "{{NL}}" in prompt:
        raise SourceFreezeError("rendered prompt retained the NL placeholder")
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


def _strict_library_nl(value: str) -> str | None:
    """Require a self-contained sentence, not merely a doc-navigation label."""

    normalized = _standalone_nl(value, mathlib_docstring=True)
    if normalized is None:
        return None
    if len(normalized) < 40 or len(_WORDS.findall(normalized)) < 7:
        return None
    if _GENERIC_LIBRARY_DOC.search(normalized):
        return None
    return normalized


def _adjacent_docstring(source: str, line_number: int) -> str | None:
    """Recover the docstring attached immediately before a census declaration."""

    starts = [0, *(match.end() for match in re.finditer("\n", source))]
    if line_number < 1 or line_number > len(starts):
        return None
    prefix = source[: starts[line_number - 1]]
    close = prefix.rfind("-/")
    if close < 0:
        return None
    start = prefix.rfind("/--", 0, close + 2)
    if start < 0:
        return None
    between = prefix[close + 2 :]
    if len(between) > 3_000 or _ATTRIBUTES_ONLY.fullmatch(between) is None:
        return None
    return prefix[start + 3 : close]


def _context_record_from_census(
    raw: Mapping[str, Any],
    *,
    source_path: Path,
    source_sha256: str,
    helper_path: Path,
    pins: RuntimePins,
) -> CompileContextRecord:
    for field in ("namespace_context", "open_context", "scoped_context"):
        values = tuple(str(value) for value in cast(Sequence[object], raw[field]))
        if "in" in values:
            raise SourceFreezeError("census context contains the parser sentinel `in`")
    namespace = tuple(str(value) for value in cast(Sequence[object], raw["namespace_context"]))
    opens = tuple(str(value) for value in cast(Sequence[object], raw["open_context"]))
    scoped = tuple(str(value) for value in cast(Sequence[object], raw["scoped_context"]))
    options = cast(dict[str, str | int | float | bool], raw["options"])
    import_header = str(raw["import_header"]).rstrip() + "\n"
    source_payload = {
        "schema_version": "sft2b_source_compile_context_v1",
        "project_id": raw["project_id"],
        "project_revision": raw["project_revision"],
        "lean_version": raw["lean_version"],
        "import_header": import_header,
        "namespace_context": list(namespace),
        "open_context": list(opens),
        "scoped_context": list(scoped),
        "options": dict(sorted(options.items())),
    }
    render_context = CompileContext(
        project_id=str(raw["project_id"]),
        project_revision=str(raw["project_revision"]),
        lean_version=str(raw["lean_version"]),
        import_header=import_header,
        command_preamble=_helper_body(helper_path, pins.sft2b_helper_hash),
        namespace_context=namespace,
        open_context=opens,
        scoped_context=scoped,
        options=options,
    )
    return CompileContextRecord(
        source_context_id=f"ctx:{hash_canonical(source_payload)}",
        render_compile_context_id=render_context.compile_context_id,
        project_id=str(raw["project_id"]),
        project_revision=str(raw["project_revision"]),
        project_path=str(raw["project_dir"]),
        lean_version=str(raw["lean_version"]),
        import_header=import_header,
        namespace_context=namespace,
        open_context=opens,
        scoped_context=scoped,
        options=options,
        source_context_path=str(source_path),
        source_context_sha256=source_sha256,
        helper_path=str(helper_path),
        helper_sha256=pins.sft2b_helper_hash,
    )


def _current_numina_context(
    *,
    import_header: str,
    opens: tuple[str, ...],
    scoped: tuple[str, ...],
    options: Mapping[str, str | int | float | bool],
    config: Mapping[str, Any],
    parquet_path: Path,
    parquet_hash: str,
    helper_path: Path,
    pins: RuntimePins,
) -> CompileContextRecord:
    raw = cast(dict[str, Any], config["current_numina"])
    payload = {
        "schema_version": "sft2b_source_compile_context_v1",
        "project_id": "mathlib",
        "project_revision": raw["project_revision"],
        "lean_version": raw["lean_version"],
        "import_header": import_header,
        "namespace_context": [],
        "open_context": list(opens),
        "scoped_context": list(scoped),
        "options": dict(sorted(options.items())),
    }
    render = CompileContext(
        project_id="mathlib",
        project_revision=str(raw["project_revision"]),
        lean_version=str(raw["lean_version"]),
        import_header=import_header,
        command_preamble=_helper_body(helper_path, pins.sft2b_helper_hash),
        open_context=opens,
        scoped_context=scoped,
        options=options,
    )
    return CompileContextRecord(
        source_context_id=f"ctx:{hash_canonical(payload)}",
        render_compile_context_id=render.compile_context_id,
        project_id="mathlib",
        project_revision=str(raw["project_revision"]),
        project_path=str(raw["project_path"]),
        lean_version=str(raw["lean_version"]),
        import_header=import_header,
        open_context=opens,
        scoped_context=scoped,
        options=dict(options),
        source_context_path=str(parquet_path),
        source_context_sha256=parquet_hash,
        helper_path=str(helper_path),
        helper_sha256=pins.sft2b_helper_hash,
    )


def _census_library_sources(
    config: Mapping[str, Any],
    *,
    config_hash: str,
    helper_path: Path,
    pins: RuntimePins,
    seed: str,
) -> tuple[list[QualifiedSource], dict[str, object]]:
    raw = cast(dict[str, Any], config["closed_library_census"])
    roots_path = Path(str(raw["roots_path"]))
    manifest_path = Path(str(raw["manifest_path"]))
    roots_hash = _require_hash(roots_path, raw["roots_sha256"], "closed library census")
    manifest_hash = _require_hash(manifest_path, raw["manifest_sha256"], "census manifest")
    manifest = _object(manifest_path)
    if manifest.get("eligible_roots_sha256") != roots_hash:
        raise SourceFreezeError("census manifest does not bind the eligible roots")
    libraries = cast(dict[str, dict[str, Any]], raw["libraries"])
    for name, library in libraries.items():
        license_path = Path(str(library["license_path"]))
        _require_hash(license_path, library["license_sha256"], f"{name} license")
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(str(library["project_path"])),
            check=True,
            capture_output=True,
            text=True,
        )
        if completed.stdout.strip() != library["revision"]:
            raise SourceFreezeError(f"{name} checkout revision drifted")
    counts: Counter[str] = Counter()
    qualified: list[QualifiedSource] = []
    file_cache: dict[Path, tuple[str, str]] = {}
    observed_sources: Counter[str] = Counter()
    for row in _read_jsonl(roots_path):
        source = str(row["source"])
        observed_sources[source] += 1
        if source not in libraries:
            counts[f"{source}:not_library_source"] += 1
            continue
        library = libraries[source]
        context_raw = cast(dict[str, Any], row["compile_context"])
        if (
            row["source_revision"] != library["revision"]
            or context_raw.get("project_revision") != library["revision"]
        ):
            counts[f"{source}:revision_mismatch"] += 1
            continue
        if any(
            value == "in"
            for key in ("namespace_context", "open_context", "scoped_context")
            for value in cast(Sequence[object], context_raw[key])
        ):
            counts[f"{source}:unsupported_context_sentinel"] += 1
            continue
        proposition = collapse_lean_whitespace(str(row["reference_signature"]))
        if not proposition or any(marker in proposition for marker in _PLACEHOLDERS):
            counts[f"{source}:placeholder_reference"] += 1
            continue
        locator = str(row["source_locator"])
        try:
            relative_path, line_raw = locator.rsplit(":", 1)
            line_number = int(line_raw)
        except ValueError:
            counts[f"{source}:bad_locator"] += 1
            continue
        source_path = Path(str(library["source_root"])) / relative_path
        cached = file_cache.get(source_path)
        if cached is None:
            try:
                source_text = source_path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                counts[f"{source}:source_read_failure"] += 1
                continue
            source_hash = hash_file(source_path)
            file_cache[source_path] = (source_text, source_hash)
        else:
            source_text, source_hash = cached
        docstring = _adjacent_docstring(source_text, line_number)
        if docstring is None:
            counts[f"{source}:missing_adjacent_docstring"] += 1
            continue
        nl = _strict_library_nl(docstring)
        if nl is None:
            counts[f"{source}:nonstandalone_or_generic_docstring"] += 1
            continue
        if _has_denied_marker(source, locator, str(library["repository_url"]), nl):
            counts[f"{source}:benchmark_family_name"] += 1
            continue
        try:
            context = _context_record_from_census(
                context_raw,
                source_path=source_path,
                source_sha256=source_hash,
                helper_path=helper_path,
                pins=pins,
            )
        except (SourceFreezeError, ValueError):
            counts[f"{source}:compile_context_rejected"] += 1
            continue
        domain = str(row["domain"])
        root_id = str(row["root_id"])
        record = _record(
            nl=nl,
            theorem_id=root_id,
            declaration_name=str(row["declaration_name"]),
            proposition=proposition,
            context=context,
            provenance=SourceProvenance(
                source_family="algebra" if domain == "algebra" else "cross_domain",
                source_url=(
                    f"{str(library['repository_url']).rstrip('/')}/blob/"
                    f"{library['revision']}/{library['repository_subdir']}/{relative_path}"
                ),
                source_revision=str(library["revision"]),
                source_path=str(source_path),
                source_file_sha256=source_hash,
                manifest_path=str(manifest_path),
                manifest_sha256=manifest_hash,
                source_recipe_sha256=config_hash,
                license_card_value=str(library["license"]),
                redistribution_note="public source; private-first SFT2B release",
                nl_extraction_rule="adjacent_human_docstring_strict_standalone_v1",
                trusted_reference_basis=(
                    "frozen zero-Lean census ConstantInfo.type with exact project/context lineage"
                ),
            ),
        )
        declaration = f"theorem source_candidate : {proposition} := by sorry"
        headless = normalize_headless(declaration)
        if headless is None:
            counts[f"{source}:headless_failure"] += 1
            continue
        audited = AuditedSource(
            source_class=f"library_{source}",
            record=record,
            headless=collapse_lean_whitespace(headless),
            near_dup_hash=signature_near_dup_hash(headless),
            problem_identity=f"{source}::{root_id}",
            selection_group=domain,
            selection_hash=_selection_hash(seed, f"library_{source}", root_id),
            complexity_score=len(proposition) + len(nl),
        )
        qualified.append(
            QualifiedSource(
                audited=audited,
                release_class=f"library_{source}",
                trust_tier="human_docstring_elaborated_reference",
                domain=domain,
                upstream_source=source,
            )
        )
        counts[f"{source}:eligible"] += 1
    expected_counts = cast(dict[str, int], manifest["source_counts"])
    if dict(observed_sources) != expected_counts:
        raise SourceFreezeError("closed-library census source counts drifted")
    return qualified, {
        "audited_rows": sum(observed_sources.values()),
        "eligible_rows": len(qualified),
        "input_source_counts": dict(sorted(observed_sources.items())),
        "outcomes": dict(sorted(counts.items())),
    }


def _current_numina_sources(
    config: Mapping[str, Any],
    *,
    config_hash: str,
    helper_path: Path,
    pins: RuntimePins,
    seed: str,
) -> tuple[list[QualifiedSource], SourceAudit]:
    raw = cast(dict[str, Any], config["current_numina"])
    readme = Path(str(raw["readme_path"]))
    parquet = Path(str(raw["parquet_path"]))
    readme_hash = _require_hash(readme, raw["readme_sha256"], "current Numina README")
    parquet_hash = _require_hash(parquet, raw["parquet_sha256"], "current Numina parquet")
    _require_hash(
        Path(str(raw["project_path"])) / "lean-toolchain",
        raw["lean_toolchain_sha256"],
        "current Numina Lean toolchain",
    )
    columns = [
        "uuid",
        "problem",
        "author",
        "formal_statement",
        "formal_ground_truth",
        "ground_truth_type",
        "formal_proof",
        "source",
        "problem_type",
        "exam",
    ]
    parquet_file = pq.ParquetFile(parquet)
    if parquet_file.metadata.num_rows != int(raw["rows"]):
        raise SourceFreezeError("current Numina row count drifted")
    counts: Counter[str] = Counter()
    qualified: list[QualifiedSource] = []
    for batch in parquet_file.iter_batches(batch_size=2_048, columns=columns):
        for row in batch.to_pylist():
            nl = _standalone_nl(str(row["problem"] or ""), mathlib_docstring=False)
            if nl is None:
                counts["nonstandalone_nl"] += 1
                continue
            author = str(row["author"] or "")
            ground_truth = str(row["formal_ground_truth"] or "")
            proof = str(row["formal_proof"] or "")
            ground_truth_type = str(row["ground_truth_type"] or "")
            if (
                author == "human"
                and ground_truth_type == "complete"
                and ground_truth.strip()
                and "sorry" not in ground_truth.casefold()
                and "admit" not in ground_truth.casefold()
            ):
                completed = ground_truth
                trust_tier = "human_complete"
            elif (
                proof.strip()
                and "sorry" not in proof.casefold()
                and "admit" not in proof.casefold()
            ):
                completed = proof
                trust_tier = "human_model_proof" if author == "human" else "auto_model_proof"
            else:
                counts["no_completed_reference"] += 1
                continue
            statement_source = str(row["formal_statement"] or "")
            try:
                statement_decl, statement_name, statement_start = _last_declaration(
                    statement_source
                )
                completed_decl, completed_name, _ = _last_declaration(completed)
            except SourceFreezeError:
                counts["declaration_parse_failure"] += 1
                continue
            if statement_name != completed_name:
                counts["declaration_name_mismatch"] += 1
                continue
            statement_headless = normalize_headless(statement_decl)
            completed_headless = normalize_headless(completed_decl)
            if statement_headless is None or completed_headless is None:
                counts["headless_failure"] += 1
                continue
            headless = collapse_lean_whitespace(statement_headless)
            if headless != collapse_lean_whitespace(completed_headless):
                counts["statement_reference_mismatch"] += 1
                continue
            parsed = _parse_header(statement_source[:statement_start])
            if parsed is None:
                counts["unsupported_context"] += 1
                continue
            import_header, opens, scoped, options = parsed
            try:
                proposition = _closed_proposition(headless)
            except SourceFreezeError:
                counts["closed_proposition_failure"] += 1
                continue
            upstream_source = str(row["source"] or "unknown")
            uuid = str(row["uuid"])
            if _has_denied_marker(
                str(raw["dataset_id"]), upstream_source, str(row["exam"] or ""), nl
            ):
                counts["benchmark_family_name"] += 1
                continue
            context = _current_numina_context(
                import_header=import_header,
                opens=opens,
                scoped=scoped,
                options=options,
                config=config,
                parquet_path=parquet,
                parquet_hash=parquet_hash,
                helper_path=helper_path,
                pins=pins,
            )
            theorem_id = f"numinamath_lean:{uuid}:{statement_name}"
            record = _record(
                nl=nl,
                theorem_id=theorem_id,
                declaration_name=statement_name,
                proposition=proposition,
                context=context,
                provenance=SourceProvenance(
                    source_family="public_research",
                    source_url=(
                        f"https://huggingface.co/datasets/{raw['dataset_id']}/blob/"
                        f"{raw['revision']}/{raw['remote_parquet_path']}"
                    ),
                    source_revision=str(raw["revision"]),
                    source_path=str(parquet),
                    source_file_sha256=parquet_hash,
                    manifest_path=str(readme),
                    manifest_sha256=readme_hash,
                    source_recipe_sha256=config_hash,
                    license_card_value=str(raw["license"]),
                    redistribution_note="public Apache-2.0 source; private-first SFT2B release",
                    nl_extraction_rule="direct_problem_field_strict_standalone_v1",
                    trusted_reference_basis=(
                        f"{trust_tier}; exact statement/completed-reference signature agreement; "
                        "dataset card reports compile verification at the pinned project revision"
                    ),
                ),
            )
            domain = str(row["problem_type"] or "unknown")
            release_class = "numina_current_human" if author == "human" else "numina_current_auto"
            audited = AuditedSource(
                source_class=release_class,
                record=record,
                headless=headless,
                near_dup_hash=signature_near_dup_hash(headless),
                problem_identity=f"numinamath_lean::{uuid}",
                selection_group=domain,
                selection_hash=_selection_hash(seed, release_class, uuid),
                complexity_score=len(proposition) + len(nl),
            )
            qualified.append(
                QualifiedSource(
                    audited=audited,
                    release_class=release_class,
                    trust_tier=trust_tier,
                    domain=domain,
                    upstream_source=upstream_source,
                )
            )
            counts[trust_tier] += 1
    return qualified, SourceAudit(int(raw["rows"]), len(qualified), counts)


def _has_denied_marker(*values: str) -> bool:
    joined = " ".join(values).casefold().replace("-", "").replace("_", "")
    return any(
        marker.replace("-", "").replace("_", "") in joined for marker in _DENIED_SOURCE_MARKERS
    )


def _wrap_existing(
    candidates: Iterable[AuditedSource],
    *,
    release_class: str,
    trust_tier: str,
) -> list[QualifiedSource]:
    wrapped: list[QualifiedSource] = []
    for item in candidates:
        if _has_denied_marker(
            item.record.provenance.source_url,
            item.record.provenance.source_path,
            item.record.nl_statement,
        ):
            continue
        wrapped.append(
            QualifiedSource(
                audited=item,
                release_class=release_class,
                trust_tier=trust_tier,
                domain=item.selection_group,
                upstream_source=item.record.provenance.source_url,
            )
        )
    return wrapped


def _screen_and_deduplicate(
    ordered: Sequence[QualifiedSource],
    *,
    golden_exact: frozenset[str],
    golden_blocklist: GoldenBlocklist,
    existing_proposition_hashes: frozenset[str],
    existing_nl: frozenset[str],
) -> tuple[list[QualifiedSource], Counter[str]]:
    selected: list[QualifiedSource] = []
    counts: Counter[str] = Counter()
    seen_source: set[str] = set()
    seen_prop: set[str] = set()
    seen_nl: set[str] = set()
    seen_near: set[str] = set()
    for value in ordered:
        item = value.audited
        source_id = item.record.source_id
        prop_hash = item.record.reference_proposition_sha256
        nl_key = _nl_key(item.record.nl_statement)
        if _has_denied_marker(
            value.release_class,
            value.upstream_source,
            item.record.provenance.source_url,
            item.record.provenance.source_path,
        ):
            counts["benchmark_family_name"] += 1
        elif item.headless in golden_exact:
            counts["golden_exact"] += 1
        elif item.near_dup_hash in golden_blocklist.near_dup_hashes:
            counts["golden_near"] += 1
        elif golden_blocklist.problem_is_blocked(item.problem_identity):
            counts["golden_problem_identity"] += 1
        elif prop_hash in existing_proposition_hashes or nl_key in existing_nl:
            counts["existing_301_overlap"] += 1
        elif source_id in seen_source:
            counts["duplicate_source_id"] += 1
        elif prop_hash in seen_prop:
            counts["duplicate_reference_proposition"] += 1
        elif nl_key in seen_nl:
            counts["duplicate_nl"] += 1
        elif item.near_dup_hash in seen_near:
            counts["duplicate_signature_near_hash"] += 1
        else:
            selected.append(value)
            seen_source.add(source_id)
            seen_prop.add(prop_hash)
            seen_nl.add(nl_key)
            seen_near.add(item.near_dup_hash)
    return selected, counts


def _deterministic_order(values: Iterable[QualifiedSource]) -> list[QualifiedSource]:
    return sorted(
        values,
        key=lambda value: (
            value.release_class,
            value.domain,
            value.audited.selection_hash,
            value.audited.record.source_id,
        ),
    )


def _matched_50k(values: Sequence[QualifiedSource], count: int) -> list[QualifiedSource]:
    if len(values) < count:
        return list(values)
    groups: dict[tuple[str, str], list[QualifiedSource]] = defaultdict(list)
    for value in values:
        groups[(value.release_class, value.domain)].append(value)
    for group in groups.values():
        group.sort(key=lambda value: (value.audited.selection_hash, value.audited.record.source_id))
    selected: list[QualifiedSource] = []
    offsets = dict.fromkeys(sorted(groups), 0)
    while len(selected) < count:
        progressed = False
        for key in sorted(groups):
            offset = offsets[key]
            if offset >= len(groups[key]):
                continue
            selected.append(groups[key][offset])
            offsets[key] += 1
            progressed = True
            if len(selected) == count:
                break
        if not progressed:
            raise SourceFreezeError("matched 50K stratification exhausted unexpectedly")
    return selected


def build_bundle(
    repo_root: Path,
    *,
    config_path: Path,
    output_dir: Path,
    limit: int | None = None,
) -> FreezeResult:
    config = _object(config_path)
    config_hash = hash_file(config_path)
    if config.get("schema_version") != "sft2b_reform_diverse_full_sources_v1":
        raise SourceFreezeError("full-source config version drifted")
    source_use = cast(dict[str, Any], config["source_use_policy"])
    _require_hash(
        repo_root / str(source_use["path"]),
        source_use["sha256"],
        "source-use policy",
    )
    helper_path = repo_root / "src/leanfaith/sft2b/lean_helper.lean"
    pins = verify_runtime_pins(repo_root, helper_path=helper_path)
    repr_raw = cast(dict[str, Any], config["repr"])
    if repr_raw != {
        "freeze_commit": pins.repr_freeze_commit,
        "spec_sha256": pins.repr_spec_hash,
        "implementation_set_sha256": pins.repr_implementation_set_hash,
        "api_sha256": pins.repr_api_hash,
    }:
        raise SourceFreezeError("REPR pins drifted")
    contamination = cast(dict[str, Any], config["contamination"])
    for path_key, hash_key in (
        ("golden_blocklist_path", "golden_blocklist_sha256"),
        ("canonical_golden_path", "canonical_golden_sha256"),
        ("benchmark_registry_path", "benchmark_registry_sha256"),
        ("benchmark_denylist_path", "benchmark_denylist_sha256"),
    ):
        path = Path(str(contamination[path_key]))
        if not path.is_absolute():
            path = repo_root / path
        _require_hash(path, contamination[hash_key], path_key)
    if contamination.get("exclude_proofnet_family") is not True:
        raise SourceFreezeError("ProofNet family exclusion must be enabled")
    existing_raw = cast(dict[str, Any], config["existing_301"])
    recipe_path = repo_root / str(existing_raw["recipe_path"])
    _require_hash(recipe_path, existing_raw["recipe_sha256"], "existing-301 recipe")
    existing, receipt = load_existing_301(
        repo_root,
        recipe_path=recipe_path,
        helper_path=helper_path,
        pins=pins,
    )
    if len(existing) != int(existing_raw["expected_candidates"]) or not receipt.all_unknown:
        raise SourceFreezeError("existing-301 replay drifted")
    existing_prop = frozenset(item.source.reference_proposition_sha256 for item in existing)
    existing_nl = frozenset(_nl_key(item.source.nl_statement) for item in existing)
    seed = str(config["selection_seed"])
    libraries, library_audit = _census_library_sources(
        config,
        config_hash=config_hash,
        helper_path=helper_path,
        pins=pins,
        seed=seed,
    )
    current_numina, current_audit = _current_numina_sources(
        config,
        config_hash=config_hash,
        helper_path=helper_path,
        pins=pins,
        seed=seed,
    )
    legacy_raw = cast(dict[str, Any], config["legacy_inputs"])
    legacy_path = repo_root / str(legacy_raw["config_path"])
    legacy_config = _object(legacy_path)
    legacy_hash = _require_hash(
        legacy_path,
        legacy_raw["config_sha256"],
        "legacy source config",
    )
    legacy_numina, legacy_audit = _numina_sources(
        legacy_config,
        config_hash=legacy_hash,
        pins=pins,
        helper_path=helper_path,
        seed=seed,
    )
    workbook, workbook_audit = _workbook_sources(
        legacy_config,
        repo_root=repo_root,
        config_hash=legacy_hash,
        pins=pins,
        helper_path=helper_path,
        seed=seed,
    )
    priority = cast(list[str], config["dedup_priority"])
    pools: dict[str, list[QualifiedSource]] = {
        "libraries": _deterministic_order(libraries),
        "workbook": _deterministic_order(
            _wrap_existing(
                workbook,
                release_class="lean_workbook",
                trust_tier="proved_no_goals",
            )
        ),
        "current_numina_human": _deterministic_order(
            value for value in current_numina if value.release_class == "numina_current_human"
        ),
        "current_numina_auto": _deterministic_order(
            value for value in current_numina if value.release_class == "numina_current_auto"
        ),
        "legacy_numina": _deterministic_order(
            _wrap_existing(
                legacy_numina,
                release_class="numina_legacy_owner",
                trust_tier="owner_valid_exact_signature",
            )
        ),
    }
    if set(priority) != set(pools):
        raise SourceFreezeError("dedup priority does not cover every source pool")
    ordered = [value for name in priority for value in pools[name]]
    blocklist_path = repo_root / str(contamination["golden_blocklist_path"])
    canonical_path = Path(str(contamination["canonical_golden_path"]))
    selected, exclusions = _screen_and_deduplicate(
        ordered,
        golden_exact=_golden_exact(canonical_path),
        golden_blocklist=GoldenBlocklist.load(blocklist_path),
        existing_proposition_hashes=existing_prop,
        existing_nl=existing_nl,
    )
    qualified_count = len(selected)
    minimum = int(config["minimum_expected_rows"])
    if qualified_count < minimum:
        raise SourceFreezeError(
            f"qualified source pool is only {qualified_count} rows, below {minimum}"
        )
    if limit is not None:
        if limit < 1:
            raise SourceFreezeError("measurement limit must be positive")
        selected = _matched_50k(selected, min(limit, qualified_count))
    selected = _deterministic_order(selected)
    rows = tuple(value.audited.record for value in selected)
    if len({row.source_id for row in rows}) != len(rows):
        raise SourceFreezeError("selected source IDs are not unique")
    placement_raw = cast(dict[str, Any], config["placement"])
    placement_path = repo_root / str(placement_raw["path"])
    placement_hash = _require_hash(placement_path, placement_raw["sha256"], "placement config")
    placement = _object(placement_path)
    if placement.get("candidate_slots") != placement_raw["slots"]:
        raise SourceFreezeError("candidate slots/seeds drifted")
    prompt_raw = cast(dict[str, Any], config["prompt"])
    prompt_path = repo_root / str(prompt_raw["path"])
    prompt_hash = _require_hash(prompt_path, prompt_raw["sha256"], "ReForm prompt")
    prompt_template = prompt_path.read_text(encoding="utf-8")
    tokenizer = _tokenizer(config)
    token_rows: list[dict[str, object]] = []
    maximum = 0
    for source in rows:
        prompt = _render_source_prompt(prompt_template, source)
        token_count = len(tokenizer.encode(prompt, add_special_tokens=True))
        maximum = max(maximum, token_count)
        token_rows.append(
            {
                "source_id": source.source_id,
                "prompt_sha256": sha256_hex(prompt.encode("utf-8")),
                "prompt_tokens": token_count,
            }
        )
    required = maximum + int(placement_raw["max_new_tokens"])
    output_dir.mkdir(parents=True, exist_ok=True)
    unexpected = {path.name for path in output_dir.iterdir() if path.is_file()} - set(OUTPUT_NAMES)
    if unexpected:
        raise SourceFreezeError(f"output directory has unexpected files: {sorted(unexpected)}")
    sources_path = output_dir / "sources.jsonl"
    with sources_path.open("wb") as handle:
        for source in rows:
            handle.write(_canonical_line(source.model_dump(mode="json")))
    counts_path = output_dir / "prompt_token_counts.json"
    counts_path.write_bytes(
        _canonical_line(
            {
                "schema_version": PROMPT_COUNTS_SCHEMA_VERSION,
                "source_count": len(rows),
                "model_id": placement_raw["model_id"],
                "model_revision": placement_raw["model_revision"],
                "prompt_sha256": prompt_hash,
                "tokenizer_revision": cast(dict[str, Any], config["tokenizer"])["revision"],
                "tokenizer_sha256": cast(dict[str, Any], config["tokenizer"])["primary_sha256"],
                "maximum_prompt_tokens": maximum,
                "max_new_tokens": placement_raw["max_new_tokens"],
                "required_max_model_len": required,
                "rows": token_rows,
            }
        )
    )
    audit_path = output_dir / "source_audit.jsonl"
    with audit_path.open("wb") as handle:
        for value in selected:
            handle.write(
                _canonical_line(
                    {
                        "schema_version": AUDIT_SCHEMA_VERSION,
                        "source_id": value.audited.record.source_id,
                        "release_class": value.release_class,
                        "trust_tier": value.trust_tier,
                        "domain": value.domain,
                        "upstream_source": value.upstream_source,
                        "selection_hash": value.audited.selection_hash,
                        "complexity_score": value.audited.complexity_score,
                        "benchmark_exact_hit": False,
                        "benchmark_near_hit": False,
                        "proofnet_family_hit": False,
                    }
                )
            )
    matched = _matched_50k(selected, int(config["matched_view_rows"]))
    matched_path = output_dir / "matched_50000_source_ids.json"
    matched_path.write_bytes(
        _canonical_line(
            {
                "schema_version": MATCHED_VIEW_SCHEMA_VERSION,
                "source_count": len(matched),
                "selection_rule": "round_robin_release_class_domain_then_frozen_hash_v1",
                "source_ids": [value.audited.record.source_id for value in matched],
            }
        )
    )
    release_mix = Counter(value.release_class for value in selected)
    trust_mix = Counter(value.trust_tier for value in selected)
    domain_mix = Counter(value.domain for value in selected)
    source_hashes = {
        name: hash_file(output_dir / name)
        for name in (
            "sources.jsonl",
            "prompt_token_counts.json",
            "source_audit.jsonl",
            "matched_50000_source_ids.json",
        )
    }
    manifest_path = output_dir / "source_manifest.json"
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "bundle_schema_version": SCHEMA_VERSION,
        "builder_git_commit": _git_head(repo_root),
        "source_config_path": str(config_path.relative_to(repo_root)),
        "source_config_sha256": config_hash,
        "source_count": len(rows),
        "qualified_source_count_before_measurement_limit": qualified_count,
        "measurement_limit": limit,
        "selection_rule": "all_quality_qualified_after_priority_ordered_global_dedup_v1",
        "dedup_priority": priority,
        "source_mix": dict(sorted(release_mix.items())),
        "trust_tier_mix": dict(sorted(trust_mix.items())),
        "domain_mix": dict(sorted(domain_mix.items())),
        "source_audits": {
            "closed_library_census": library_audit,
            "current_numina": current_audit.to_dict(),
            "legacy_numina": legacy_audit.to_dict(),
            "lean_workbook": workbook_audit.to_dict(),
        },
        "raw_qualified_pool_counts": {name: len(values) for name, values in sorted(pools.items())},
        "contamination_and_dedup_exclusions": dict(sorted(exclusions.items())),
        "benchmark_exclusions": {
            "proofnet_family": (
                "blanket excluded: ProofNet, ProofNetVerif, ProofNetSharp, ProofNet#, "
                "and derived/mixed variants"
            ),
            "shadowbench": "excluded reference-free test-only",
            "golden_exact_and_near": "excluded using frozen blocklist and canonical gold",
            "selected_exact_hits": 0,
            "selected_near_hits": 0,
            "selected_proofnet_family_hits": 0,
        },
        "audited_but_not_admitted_catalogs": config["audited_not_admitted"],
        "existing_301_replay": {
            **existing_raw,
            "observed_candidates": len(existing),
            "all_semantic_labels_unknown": receipt.all_unknown,
        },
        "source_catalogs": {
            "closed_library_census": config["closed_library_census"],
            "current_numina": config["current_numina"],
            "legacy_inputs": config["legacy_inputs"],
        },
        "source_use_policy": config["source_use_policy"],
        "schemas": {
            "source_record": "sft2b_source_v1",
            "source_record_json_schema_sha256": hash_canonical(SourceRecord.model_json_schema()),
            "source_audit": AUDIT_SCHEMA_VERSION,
            "prompt_token_counts": PROMPT_COUNTS_SCHEMA_VERSION,
            "matched_view": MATCHED_VIEW_SCHEMA_VERSION,
        },
        "prompt": {**prompt_raw, "observed_sha256": prompt_hash},
        "tokenizer": config["tokenizer"],
        "placement": {
            "path": placement_raw["path"],
            "sha256": placement_hash,
            "model_id": placement_raw["model_id"],
            "model_revision": placement_raw["model_revision"],
            "candidate_slots": placement_raw["slots"],
            "max_new_tokens": placement_raw["max_new_tokens"],
            "required_max_model_len": required,
        },
        "repr": {**repr_raw, "verified_runtime_pins": pins.to_dict()},
        "prompt_tokens": {
            "maximum_prompt_tokens": maximum,
            "max_new_tokens": placement_raw["max_new_tokens"],
            "required_max_model_len": required,
        },
        "matched_50000_view": {
            "requested_rows": int(config["matched_view_rows"]),
            "actual_rows": len(matched),
        },
        "data_files": {
            name: {
                "sha256": digest,
                "rows": len(rows)
                if name in {"sources.jsonl", "source_audit.jsonl"}
                else len(token_rows)
                if name == "prompt_token_counts.json"
                else len(matched),
            }
            for name, digest in sorted(source_hashes.items())
        },
    }
    manifest_path.write_bytes(_canonical_line(manifest))
    checksums = {**source_hashes, "source_manifest.json": hash_file(manifest_path)}
    checksum_path = output_dir / "SHA256SUMS"
    checksum_path.write_text(
        "".join(f"{digest}  {name}\n" for name, digest in sorted(checksums.items())),
        encoding="utf-8",
    )
    result = FreezeResult(
        output_dir=output_dir,
        rows=rows,
        source_mix=dict(sorted(release_mix.items())),
        maximum_prompt_tokens=maximum,
        required_max_model_len=required,
        file_sha256={name: hash_file(output_dir / name) for name in OUTPUT_NAMES},
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
    names = {path.name for path in bundle_dir.iterdir() if path.is_file()}
    if names != set(OUTPUT_NAMES):
        raise SourceFreezeError(f"bundle file set mismatch: {sorted(names)}")
    expected: dict[str, str] = {}
    for line in (bundle_dir / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (\S+)", line)
        if match is None:
            raise SourceFreezeError("malformed SHA256SUMS")
        expected[match.group(2)] = match.group(1)
    if set(expected) != set(OUTPUT_NAMES) - {"SHA256SUMS"}:
        raise SourceFreezeError("SHA256SUMS coverage drifted")
    for name, digest in expected.items():
        if hash_file(bundle_dir / name) != digest:
            raise SourceFreezeError(f"checksum mismatch: {name}")
    rows = tuple(
        SourceRecord.model_validate(row) for row in _read_jsonl(bundle_dir / "sources.jsonl")
    )
    if len(rows) != len({row.source_id for row in rows}):
        raise SourceFreezeError("source rows are not unique")
    if any(
        _has_denied_marker(
            row.provenance.source_url,
            row.provenance.source_path,
            row.provenance.trusted_reference_basis,
        )
        for row in rows
    ):
        raise SourceFreezeError("ProofNet/ShadowBench family marker leaked into sources")
    config = _object(config_path)
    token_payload = _object(bundle_dir / "prompt_token_counts.json")
    token_rows = cast(list[dict[str, Any]], token_payload["rows"])
    if len(token_rows) != len(rows):
        raise SourceFreezeError("prompt-token row count mismatch")
    prompt_raw = cast(dict[str, Any], config["prompt"])
    prompt_path = repo_root / str(prompt_raw["path"])
    _require_hash(prompt_path, prompt_raw["sha256"], "ReForm prompt")
    template = prompt_path.read_text(encoding="utf-8")
    tokenizer = _tokenizer(config, snapshot_override=tokenizer_snapshot_path)
    maximum = 0
    for source, token_row in zip(rows, token_rows, strict=True):
        prompt = _render_source_prompt(template, source)
        observed_tokens = len(tokenizer.encode(prompt, add_special_tokens=True))
        if (
            token_row.get("source_id") != source.source_id
            or token_row.get("prompt_sha256") != sha256_hex(prompt.encode("utf-8"))
            or token_row.get("prompt_tokens") != observed_tokens
        ):
            raise SourceFreezeError(f"prompt replay mismatch: {source.source_id}")
        maximum = max(maximum, observed_tokens)
    required = maximum + int(cast(dict[str, Any], config["placement"])["max_new_tokens"])
    if (
        token_payload.get("maximum_prompt_tokens") != maximum
        or token_payload.get("required_max_model_len") != required
    ):
        raise SourceFreezeError("prompt maximum replay mismatch")
    audit_rows = list(_read_jsonl(bundle_dir / "source_audit.jsonl"))
    if [row.source_id for row in rows] != [str(row["source_id"]) for row in audit_rows]:
        raise SourceFreezeError("source/audit ordering mismatch")
    manifest = _object(bundle_dir / "source_manifest.json")
    if manifest.get("source_count") != len(rows):
        raise SourceFreezeError("manifest source count mismatch")
    source_mix = dict(Counter(str(row["release_class"]) for row in audit_rows))
    if manifest.get("source_mix") != dict(sorted(source_mix.items())):
        raise SourceFreezeError("manifest source mix mismatch")
    matched = _object(bundle_dir / "matched_50000_source_ids.json")
    matched_ids = cast(list[str], matched["source_ids"])
    if len(matched_ids) != len(set(matched_ids)) or not set(matched_ids).issubset(
        {row.source_id for row in rows}
    ):
        raise SourceFreezeError("matched 50K view is invalid")
    return FreezeResult(
        output_dir=bundle_dir,
        rows=rows,
        source_mix=dict(sorted(source_mix.items())),
        maximum_prompt_tokens=maximum,
        required_max_model_len=required,
        file_sha256={name: hash_file(bundle_dir / name) for name in OUTPUT_NAMES},
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("configs/sft2b/reform_diverse_full_sources_v1.json"),
    )
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--verify-dir", type=Path)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--tokenizer-snapshot-path", type=Path)
    return parser


def main() -> None:
    args = _parser().parse_args()
    repo_root = Path.cwd()
    config_path = args.config if args.config.is_absolute() else repo_root / args.config
    if (args.output_dir is None) == (args.verify_dir is None):
        raise SystemExit("provide exactly one of --output-dir or --verify-dir")
    if args.output_dir is not None:
        result = build_bundle(
            repo_root,
            config_path=config_path,
            output_dir=args.output_dir,
            limit=args.limit,
        )
    else:
        result = verify_bundle(
            repo_root,
            config_path=config_path,
            bundle_dir=args.verify_dir,
            tokenizer_snapshot_path=args.tokenizer_snapshot_path,
        )
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
