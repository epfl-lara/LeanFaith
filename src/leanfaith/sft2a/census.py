"""Zero-Lean source census and deterministic source/domain/shape sampler for SFT2A v5."""

# ruff: noqa: RUF001

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.cpt2.splitters import mask_lean_source
from leanfaith.representations.views import signature_near_dup_hash
from leanfaith.sft2a.config import LoadedSFT2AConfig
from leanfaith.sft2a.legacy import _atomic_exact, _blocklist
from leanfaith.sft2a.mechanisms import (
    applicable_mechanisms,
    plan_mechanism_rotation,
    signature_shape,
)
from leanfaith.sft2a.models import CompileContextConfig, OneRootConfig, SFT2AV5Config

_VERSION = "sft2a_zero_lean_source_census_v5"
_DECLARATION = re.compile(
    r"(?m)^[ \t]*(?:(?:private|protected|noncomputable|unsafe|local)\s+)*"
    r"(?:theorem|lemma)\s+([^\s:({\[]+)"
)
_NAMESPACE = re.compile(r"^\s*namespace\s+([A-Za-z_][A-Za-z0-9_'.]*)\s*$")
_END = re.compile(r"^\s*end(?:\s+([A-Za-z_][A-Za-z0-9_'.]*))?\s*$")
_OPEN = re.compile(r"^\s*open\s+(?!scoped\b)([A-Za-z0-9_'. ]+)\s*$")
_OPEN_SCOPED = re.compile(r"^\s*open\s+scoped\s+([A-Za-z0-9_'. ]+)\s*$")
_SECTION = re.compile(r"^\s*section(?:\s+([A-Za-z_][A-Za-z0-9_'.]*))?\s*$")
_VARIABLE = re.compile(r"^\s*variable\b")
_FORBIDDEN = re.compile(r"\b(?:sorry|admit|axiom)\b|\[anonymous\]|⋯|\.\.\.", re.I)
_DOMAIN_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("number_theory", ("Prime", "Nat.gcd", "factorial", "Dvd", "∣")),
    ("analysis", ("Continuous", "Tendsto", "Filter", "deriv", "integral", "ℝ")),
    ("set_theory", ("Set", "Finset", "∈", "⊆", "∪", "∩")),
    ("order", ("≤", "<", "Preorder", "Lattice", "Order")),
    ("logic", ("Derivable", "Satisfies", "Theory", "¬", "↔")),
    ("computability", ("Program", "Machine", "Step", "Language", "Comput")),
    ("probability_ml", ("Measure", "Probability", "ConceptClass", "Learner")),
    ("physics", ("Space", "Time", "Energy", "Matrix", "Lorentz", "Particle")),
    ("algebra", ("Monoid", "Group", "Ring", "Field", "+", "*", "^")),
)


class CensusError(RuntimeError):
    """A zero-Lean census input, declaration, or deterministic sample failed."""


@dataclass(slots=True)
class _ScopeState:
    kind: str
    name: str | None
    has_variables: bool = False


def _jsonl(rows: Iterable[Mapping[str, object]]) -> bytes:
    return b"".join(canonical_json_bytes(dict(row)) + b"\n" for row in rows)


def _assignment_offset(masked: str, start: int, finish: int) -> int | None:
    stack: list[str] = []
    pairs = {")": "(", "]": "[", "}": "{"}
    index = start
    while index + 1 < finish:
        char = masked[index]
        if char in "([{":
            stack.append(char)
        elif char in ")]}":
            if stack and stack[-1] == pairs[char]:
                stack.pop()
        elif not stack and masked.startswith(":=", index):
            return index
        index += 1
    return None


def _signature_from_declaration(
    source: str,
    start: int,
    name_end: int,
    finish: int,
    *,
    masked: str | None = None,
) -> str | None:
    masked = mask_lean_source(source) if masked is None else masked
    assignment = _assignment_offset(masked, name_end, finish)
    if assignment is None:
        return None
    raw_header = source[name_end:assignment]
    if "--" in raw_header or "/-" in raw_header:
        return None
    leading = len(raw_header) - len(raw_header.lstrip())
    header = raw_header.strip()
    masked_header = masked[name_end:assignment][leading : leading + len(header)]
    stack: list[str] = []
    separator: int | None = None
    pairs = {")": "(", "]": "[", "}": "{"}
    for offset, char in enumerate(masked_header):
        if char in "([{":
            stack.append(char)
        elif char in ")]}":
            if stack and stack[-1] == pairs[char]:
                stack.pop()
        elif char == ":" and not stack:
            separator = offset
            break
    if separator is None:
        return None
    binders = header[:separator].strip()
    conclusion = header[separator + 1 :].strip()
    if not conclusion or any(
        token in conclusion for token in ("termination_by", "decreasing_by", " where ")
    ):
        return None
    signature = f"∀ {binders}, {conclusion}" if binders else conclusion
    signature = " ".join(signature.split())
    if not 20 <= len(signature) <= 5000 or _FORBIDDEN.search(signature):
        return None
    return signature


def _contexts_at_offsets(
    source: str, offsets: Sequence[int]
) -> dict[int, tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], bool]]:
    wanted = sorted(offsets)
    result: dict[int, tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], bool]] = {}
    scopes: list[_ScopeState] = []
    root_has_variables = False
    opened: set[str] = set()
    scoped: set[str] = set()
    position = 0
    cursor = 0
    for line in source.splitlines(keepends=True):
        while cursor < len(wanted) and wanted[cursor] < position + len(line):
            offset = wanted[cursor]
            namespaces = tuple(
                scope.name
                for scope in scopes
                if scope.kind == "namespace" and scope.name is not None
            )
            result[offset] = (
                namespaces,
                tuple(sorted(opened)),
                tuple(sorted(scoped)),
                root_has_variables or any(scope.has_variables for scope in scopes),
            )
            cursor += 1
        namespace = _NAMESPACE.match(line)
        section = _SECTION.match(line)
        end = _END.match(line)
        open_match = _OPEN.match(line)
        scoped_match = _OPEN_SCOPED.match(line)
        if namespace:
            scopes.append(_ScopeState("namespace", namespace.group(1)))
        elif section:
            scopes.append(_ScopeState("section", section.group(1)))
        elif end and scopes:
            expected = end.group(1)
            if expected is None:
                scopes.pop()
            else:
                matching = next(
                    (
                        index
                        for index in range(len(scopes) - 1, -1, -1)
                        if scopes[index].name == expected
                    ),
                    len(scopes) - 1,
                )
                del scopes[matching:]
        elif _VARIABLE.match(line):
            if scopes:
                scopes[-1].has_variables = True
            else:
                root_has_variables = True
        elif scoped_match:
            names = scoped_match.group(1).split()
            scoped.update(names[:-1] if names and names[-1] == "in" else names)
        elif open_match:
            names = open_match.group(1).split()
            opened.update(names[:-1] if names and names[-1] == "in" else names)
        position += len(line)
    for offset in wanted[cursor:]:
        namespaces = tuple(
            scope.name for scope in scopes if scope.kind == "namespace" and scope.name is not None
        )
        result[offset] = (
            namespaces,
            tuple(sorted(opened)),
            tuple(sorted(scoped)),
            root_has_variables or any(scope.has_variables for scope in scopes),
        )
    return result


def _domain(signature: str, source: str, locator: str) -> str:
    if source == "mathlib":
        top = locator.split("/", maxsplit=1)[0]
        return {
            "Algebra": "algebra",
            "Analysis": "analysis",
            "CategoryTheory": "category_theory",
            "Combinatorics": "combinatorics",
            "Data": "data_structures",
            "FieldTheory": "field_theory",
            "Geometry": "geometry",
            "LinearAlgebra": "linear_algebra",
            "NumberTheory": "number_theory",
            "Order": "order",
            "Probability": "probability",
            "SetTheory": "set_theory",
            "Topology": "topology",
        }.get(top, "general_mathlib")
    if source == "physlib":
        for prefix, domain in (
            ("ClassicalMechanics/", "classical_mechanics"),
            ("Cosmology/", "cosmology"),
            ("Particles/", "particle_physics"),
            ("QFT/", "quantum_field_theory"),
            ("QuantumMechanics/", "quantum_mechanics"),
            ("SpaceAndTime/", "spacetime_geometry"),
        ):
            if locator.startswith(prefix):
                return domain
        return "physics"
    if source == "cslib":
        for prefix, domain in (
            ("Algorithms/", "algorithms"),
            ("Computability/", "computability"),
            ("Logics/", "logic"),
            ("MachineLearning/", "machine_learning"),
        ):
            if locator.startswith(prefix):
                return domain
        return "computer_science"
    text = f"{signature} {locator}"
    for domain, tokens in _DOMAIN_RULES:
        if any(token in text for token in tokens):
            return domain
    return "compiler_math" if source == "compiler_data" else "general"


def _context(
    base: CompileContextConfig,
    *,
    namespaces: tuple[str, ...],
    opened: tuple[str, ...],
    scoped: tuple[str, ...],
) -> CompileContextConfig:
    options = dict(base.options)
    options["autoImplicit"] = True
    return base.model_copy(
        update={
            "namespace_context": namespaces,
            "open_context": opened,
            "scoped_context": scoped,
            "options": options,
        }
    )


def _library_rows(
    *,
    source_name: str,
    source_root: Path,
    source_revision: str,
    base_context: CompileContextConfig,
) -> Iterable[dict[str, object]]:
    for path in sorted(source_root.rglob("*.lean")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        masked = mask_lean_source(source)
        matches = list(_DECLARATION.finditer(masked))
        contexts = _contexts_at_offsets(source, [match.start() for match in matches])
        for index, match in enumerate(matches):
            finish = matches[index + 1].start() if index + 1 < len(matches) else len(source)
            signature = _signature_from_declaration(
                source,
                match.start(),
                match.end(),
                finish,
                masked=masked,
            )
            if signature is None:
                continue
            namespace, opened, scoped, has_context_variables = contexts[match.start()]
            if has_context_variables:
                continue
            declaration_name = match.group(1)
            qualified = ".".join((*namespace, declaration_name))
            relative = path.relative_to(source_root)
            line = source.count("\n", 0, match.start()) + 1
            locator = f"{relative}:{line}"
            context = _context(
                base_context,
                namespaces=namespace,
                opened=opened,
                scoped=scoped,
            )
            root_id = f"{source_name}:census:{sha256_hex(f'{locator}:{qualified}'.encode())[:24]}"
            yield {
                "root_id": root_id,
                "source": source_name,
                "source_revision": source_revision,
                "source_license": "Apache-2.0",
                "declaration_name": qualified,
                "reference_signature": signature,
                "compile_context": context.model_dump(mode="json"),
                "source_locator": locator,
                "domain": _domain(signature, source_name, locator),
                "shape_id": signature_shape(signature).shape_id,
            }


def _compiler_rows(path: Path, base_context: CompileContextConfig) -> Iterable[dict[str, object]]:
    options = dict(base_context.options)
    options["autoImplicit"] = True
    safe_context = base_context.model_copy(update={"options": options})
    with path.open(encoding="utf-8") as handle:
        for index, line in enumerate(handle):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(raw, dict) or raw.get("label") is not True:
                continue
            theorem = raw.get("theorem")
            if not isinstance(theorem, str):
                continue
            matches = list(_DECLARATION.finditer(mask_lean_source(theorem)))
            if not matches:
                continue
            match = matches[-1]
            signature = _signature_from_declaration(
                theorem, match.start(), match.end(), len(theorem)
            )
            if signature is None:
                continue
            declaration_name = match.group(1)
            theorem_hash = sha256_hex(theorem.encode())
            locator = f"compiler_data:{index}:theorem_sha256={theorem_hash}"
            yield {
                "root_id": f"compiler_data:census:{theorem_hash[:24]}",
                "source": "compiler_data",
                "source_revision": "ca37d4701b11022f183e72b7b96ff543a8a615d3",
                "source_license": "Apache-2.0",
                "declaration_name": declaration_name,
                "reference_signature": signature,
                "compile_context": safe_context.model_dump(mode="json"),
                "source_locator": locator,
                "domain": _domain(signature, "compiler_data", locator),
                "shape_id": signature_shape(signature).shape_id,
            }


def _base_contexts(loaded: LoadedSFT2AConfig) -> dict[str, CompileContextConfig]:
    catalog = loaded.repo_root / "configs/sft2a/pilot_root_catalog_v2.json"
    raw = json.loads(catalog.read_text(encoding="utf-8"))
    contexts = raw.get("contexts") if isinstance(raw, dict) else None
    if not isinstance(contexts, dict):
        raise CensusError("frozen pilot catalog lacks reusable compile contexts")
    return {
        "mathlib": CompileContextConfig.model_validate(contexts["mathlib_full"]),
        "physlib": CompileContextConfig.model_validate(contexts["physlib_full"]),
        "cslib": CompileContextConfig.model_validate(contexts["cslib_full"]),
    }


def run_zero_lean_census(
    loaded: LoadedSFT2AConfig,
    *,
    output_root: Path | None = None,
) -> dict[str, object]:
    """Scan sources and write an immutable eligible-root inventory without Lean/providers."""

    if not isinstance(loaded.config, SFT2AV5Config):
        raise CensusError("v5 census requires the additive closure-aware config")
    output = (
        output_root or Path(loaded.config.staging_root) / loaded.config.source_census.output_subdir
    )
    manifest_path = output / "manifest.json"
    if manifest_path.is_file():
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        inventory = output / "eligible_roots.jsonl"
        if not isinstance(existing, dict) or hash_file(inventory) != existing.get(
            "eligible_roots_sha256"
        ):
            raise CensusError("immutable census replay differs")
        return dict(existing)
    contexts = _base_contexts(loaded)
    policy = loaded.config.source_census
    producers = [
        _library_rows(
            source_name=name,
            source_root=Path(policy.library_source_subdirs[name]),
            source_revision=contexts[name].project_revision,
            base_context=contexts[name],
        )
        for name in ("mathlib", "physlib", "cslib")
    ]
    producers.append(_compiler_rows(Path(policy.compiler_data_path), contexts["mathlib"]))
    _blocklist_path, blocked_hashes = _blocklist(loaded)
    seen: set[str] = set()
    eligible: list[dict[str, object]] = []
    rejected: Counter[str] = Counter()
    for rows in producers:
        for row in rows:
            signature = str(row["reference_signature"])
            normalized = " ".join(signature.split())
            if normalized in seen:
                rejected["duplicate_signature"] += 1
                continue
            if signature_near_dup_hash(signature) in blocked_hashes:
                rejected["gold_contamination"] += 1
                continue
            seen.add(normalized)
            eligible.append(row)
    eligible.sort(key=lambda row: (str(row["source"]), str(row["root_id"])))
    payload = _jsonl(eligible)
    _atomic_exact(output / "eligible_roots.jsonl", payload)
    source_counts = Counter(str(row["source"]) for row in eligible)
    stratum_counts = Counter(
        (str(row["source"]), str(row["domain"]), str(row["shape_id"])) for row in eligible
    )
    census_document: dict[str, object] = {
        "version": _VERSION,
        "config_hash": loaded.config_hash,
        "input_bindings": {
            "compiler_data": {
                "path": policy.compiler_data_path,
                "sha256": policy.compiler_data_sha256,
            },
            "library_source_subdirs": policy.library_source_subdirs,
            "project_revisions": {
                name: contexts[name].project_revision for name in sorted(contexts)
            },
        },
        "source_filter_contract": {
            "section_or_namespace_variable_context": "exclude_before_sampling",
            "open_command_trailing_in": "strip_context_modifier_token",
            "lean_requests": 0,
        },
        "eligible_rows": len(eligible),
        "source_counts": dict(sorted(source_counts.items())),
        "source_domain_shape_strata": [
            {"source": source, "domain": domain, "shape_id": shape, "rows": count}
            for (source, domain, shape), count in sorted(stratum_counts.items())
        ],
        "rejected": dict(sorted(rejected.items())),
        "eligible_roots_sha256": hash_file(output / "eligible_roots.jsonl"),
        "provider_calls_executed": 0,
        "lean_requests_executed": 0,
    }
    _atomic_exact(manifest_path, canonical_json_bytes(census_document) + b"\n")
    return census_document


def _stratified_select(
    rows: Sequence[dict[str, object]], *, count: int, salt: str
) -> list[dict[str, object]]:
    strata: dict[tuple[str, str], list[tuple[str, dict[str, object]]]] = defaultdict(list)
    for row in rows:
        key = (str(row["domain"]), str(row["shape_id"]))
        rank = hash_canonical({"salt": salt, "root_id": row["root_id"], "stratum": key})
        strata[key].append((rank, row))
    for stratum_rows in strata.values():
        stratum_rows.sort(key=lambda item: item[0])
    by_domain: dict[str, list[dict[str, object]]] = defaultdict(list)
    domain_shapes: dict[str, list[str]] = defaultdict(list)
    for domain, shape in sorted(strata):
        domain_shapes[domain].append(shape)
    for domain in sorted(domain_shapes):
        shapes = domain_shapes[domain]
        cursor = 0
        while True:
            progressed = False
            for shape in shapes:
                stratum_rows = strata[(domain, shape)]
                if cursor < len(stratum_rows):
                    by_domain[domain].append(stratum_rows[cursor][1])
                    progressed = True
            if not progressed:
                break
            cursor += 1
    selected: list[dict[str, object]] = []
    ordered = sorted(by_domain)
    cursor = 0
    while len(selected) < count:
        progressed = False
        for domain in ordered:
            domain_rows = by_domain[domain]
            if cursor < len(domain_rows):
                selected.append(domain_rows[cursor])
                progressed = True
                if len(selected) == count:
                    break
        if not progressed:
            raise CensusError("source census has too few roots for a rehearsal allocation")
        cursor += 1
    return selected


def prepare_rehearsal_sample(
    loaded: LoadedSFT2AConfig,
    *,
    census_root: Path | None = None,
    output_root: Path | None = None,
) -> dict[str, object]:
    """Freeze the 100-root sample, mechanism plan, and resumable project shards."""

    if not isinstance(loaded.config, SFT2AV5Config):
        raise CensusError("v5 rehearsal sampling requires the additive closure-aware config")
    census = (
        census_root or Path(loaded.config.staging_root) / loaded.config.source_census.output_subdir
    )
    census_manifest = run_zero_lean_census(loaded, output_root=census)
    output = output_root or Path(loaded.config.staging_root) / loaded.config.rehearsal.output_subdir
    manifest_path = output / "sample_manifest.json"
    if manifest_path.is_file():
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(existing, dict) or hash_file(output / "sample.jsonl") != existing.get(
            "sample_sha256"
        ):
            raise CensusError("immutable v5 rehearsal sample differs")
        return dict(existing)
    rows = [
        json.loads(line)
        for line in (census / "eligible_roots.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    by_source: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        if isinstance(row, dict):
            signature = str(row["reference_signature"])
            if applicable_mechanisms(signature, "preserving") and applicable_mechanisms(
                signature, "breaking"
            ):
                by_source[str(row["source"])].append(row)
    selected: list[dict[str, object]] = []
    for allocation in loaded.config.rehearsal.allocations:
        selected.extend(
            _stratified_select(
                by_source[allocation.source],
                count=allocation.roots,
                salt=f"{loaded.config.rehearsal.salt}:{allocation.source}",
            )
        )
    rotation = plan_mechanism_rotation(
        [{"root": row} for row in selected],
        salt=loaded.config.mechanism_rotation.salt,
        maximum_family_fraction_per_polarity=(
            loaded.config.mechanism_rotation.maximum_family_fraction_per_polarity
        ),
    )
    sample_rows: list[dict[str, object]] = []
    for row in selected:
        root_config = OneRootConfig.model_validate(
            {
                **{
                    key: row[key]
                    for key in (
                        "root_id",
                        "source",
                        "source_revision",
                        "source_license",
                        "declaration_name",
                        "reference_signature",
                        "compile_context",
                    )
                },
                "external_transmission": True,
                "policy_version": "source_use_v2",
                "expected_reference_goal_v1": "UNVERIFIED_UNTIL_AUTHORIZED_REHEARSAL",
            }
        )
        sample_rows.append(
            {
                "root": root_config.model_dump(mode="json"),
                "source_locator": row["source_locator"],
                "domain": row["domain"],
                "shape_id": row["shape_id"],
                "mechanism_plan": {
                    slot_id: assignment.to_dict()
                    for slot_id, assignment in sorted(rotation[root_config.root_id].items())
                },
            }
        )
    sample_rows.sort(
        key=lambda row: (
            str(row["root"]["compile_context"]["project_id"]),  # type: ignore[index]
            str(row["root"]["root_id"]),  # type: ignore[index]
        )
    )
    _atomic_exact(output / "sample.jsonl", _jsonl(sample_rows))
    shard_receipts: list[dict[str, object]] = []
    roots_per_shard = loaded.config.rehearsal.roots_per_shard
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in sample_rows:
        root_document = row["root"]
        assert isinstance(root_document, dict)
        context = root_document["compile_context"]
        assert isinstance(context, dict)
        grouped[str(context["project_id"])].append(row)
    for project_id in sorted(grouped):
        project_rows = grouped[project_id]
        for start in range(0, len(project_rows), roots_per_shard):
            shard = project_rows[start : start + roots_per_shard]
            shard_id = f"{project_id}-{start // roots_per_shard:03d}"
            relative = Path("shards") / project_id / f"{shard_id}.jsonl"
            _atomic_exact(output / relative, _jsonl(shard))
            shard_receipts.append(
                {
                    "shard_id": shard_id,
                    "project_id": project_id,
                    "path": relative.as_posix(),
                    "sha256": hash_file(output / relative),
                    "root_count": len(shard),
                }
            )
    source_counts = Counter(str(row["root"]["source"]) for row in sample_rows)  # type: ignore[index]
    stratum_counts = Counter(
        (str(row["root"]["source"]), str(row["domain"]), str(row["shape_id"]))  # type: ignore[index]
        for row in sample_rows
    )
    sample_document: dict[str, object] = {
        "version": "leanfaith_sft2a_rehearsal_sample_v5",
        "config_hash": loaded.config_hash,
        "census_manifest_sha256": hash_file(census / "manifest.json"),
        "census_inventory_sha256": census_manifest["eligible_roots_sha256"],
        "sampler_version": loaded.config.rehearsal.sampler_version,
        "salt": loaded.config.rehearsal.salt,
        "root_count": len(sample_rows),
        "slot_count": len(sample_rows) * 4,
        "source_mix": dict(sorted(source_counts.items())),
        "source_domain_shape_strata": [
            {"source": source, "domain": domain, "shape_id": shape, "roots": count}
            for (source, domain, shape), count in sorted(stratum_counts.items())
        ],
        "selected_root_ids": [str(row["root"]["root_id"]) for row in sample_rows],  # type: ignore[index]
        "sample_sha256": hash_file(output / "sample.jsonl"),
        "shards": shard_receipts,
        "ceilings": loaded.config.rehearsal.ceilings.model_dump(mode="json"),
        "rehearsal_authorized": False,
        "provider_calls_executed": 0,
        "lean_requests_executed": 0,
        "scale_10k_authorized": False,
        "scale_50k_authorized": False,
        "published": False,
    }
    _atomic_exact(manifest_path, canonical_json_bytes(sample_document) + b"\n")
    return sample_document


def loaded_with_root(loaded: LoadedSFT2AConfig, root: OneRootConfig) -> LoadedSFT2AConfig:
    config = loaded.config.model_copy(update={"root": root})
    return replace(
        loaded,
        config=config,
        config_hash=hash_canonical(config.model_dump(mode="json")),
    )


__all__ = [
    "CensusError",
    "loaded_with_root",
    "prepare_rehearsal_sample",
    "run_zero_lean_census",
]
