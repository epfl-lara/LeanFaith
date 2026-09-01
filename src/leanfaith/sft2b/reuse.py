"""Hash-pinned recovery of the 301 existing SFT2B candidate inputs."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from leanfaith.config.hashing import canonical_json_bytes, hash_file, sha256_hex
from leanfaith.sft2b.lean import compile_context_from_source
from leanfaith.sft2b.pins import RuntimePins
from leanfaith.sft2b.schemas import (
    CandidateOrigin,
    CandidateRecord,
    CandidateSlot,
    CompileContextRecord,
    FormalizerLineage,
    SourceProvenance,
    SourceRecord,
    stable_id,
)


class Existing301Error(RuntimeError):
    """Raised when the accepted DATA-REUSE recipe or consumed bytes drift."""


@dataclass(frozen=True, slots=True)
class FileBinding:
    path: str
    sha256: str

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class ExistingCandidate:
    source: SourceRecord
    candidate: CandidateRecord
    pair_path: str
    pair_path_sha256: str
    manifest_path: str
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class Existing301Receipt:
    schema_version: str
    recipe_path: str
    recipe_sha256: str
    data_reuse_tree_sha256: str
    manifests: tuple[FileBinding, ...]
    consumed_files: tuple[FileBinding, ...]
    consumed_bundle_sha256: str
    candidate_count: int
    unique_reference_count: int
    family_counts: dict[str, int]
    all_unknown: bool
    superseded_public_tranches_excluded: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "recipe_path": self.recipe_path,
            "recipe_sha256": self.recipe_sha256,
            "data_reuse_tree_sha256": self.data_reuse_tree_sha256,
            "manifests": [item.to_dict() for item in self.manifests],
            "consumed_files": [item.to_dict() for item in self.consumed_files],
            "consumed_bundle_sha256": self.consumed_bundle_sha256,
            "candidate_count": self.candidate_count,
            "unique_reference_count": self.unique_reference_count,
            "family_counts": dict(sorted(self.family_counts.items())),
            "all_unknown": self.all_unknown,
            "superseded_public_tranches_excluded": self.superseded_public_tranches_excluded,
        }


def _object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise Existing301Error(f"expected JSON object: {path}")
    return cast(dict[str, Any], value)


def _one_jsonl(path: Path) -> dict[str, Any]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    if len(rows) != 1 or not isinstance(rows[0], dict):
        raise Existing301Error(f"expected exactly one JSON object row: {path}")
    return cast(dict[str, Any], rows[0])


def _iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise Existing301Error(f"non-object row at {path}:{number}")
            yield cast(dict[str, Any], value)


def _resolve(repo_root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else repo_root / path


def _binding(repo_root: Path, path: Path) -> FileBinding:
    try:
        relative = path.relative_to(repo_root).as_posix()
    except ValueError:
        relative = str(path)
    return FileBinding(path=relative, sha256=hash_file(path))


def _binding_hash(bindings: list[FileBinding]) -> str:
    digest = hashlib.sha256()
    for item in sorted(bindings, key=lambda value: value.path):
        digest.update(item.path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(item.sha256.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _family(path: Path) -> str:
    parts = set(path.parts)
    if "public_research_v1" in parts:
        return "public_research"
    if "cross_domain_docstrings_operational_v1" in parts:
        return "cross_domain"
    return "algebra"


def _canonical_manifests(repo_root: Path, recipe: dict[str, Any]) -> list[Path]:
    result: list[Path] = []
    for root_value in recipe["canonical_roots"]:
        root = _resolve(repo_root, str(root_value))
        for manifest in sorted(root.glob("**/postprocess_v*/manifest.json")):
            if root.name == "public_research_v1" and manifest.parent.name != str(
                recipe["public_research_postprocess"]
            ):
                continue
            result.append(manifest)
    return result


def _reference_catalogs(
    repo_root: Path, recipe: dict[str, Any], consumed: list[FileBinding]
) -> dict[str, dict[str, dict[str, Any]]]:
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for family, raw_spec in dict(recipe["reference_catalogs"]).items():
        spec = cast(dict[str, Any], raw_spec)
        path = _resolve(repo_root, str(spec["path"]))
        if hash_file(path) != spec["sha256"]:
            raise Existing301Error(f"reference catalog hash mismatch: {path}")
        rows = list(_iter_jsonl(path))
        if len(rows) != int(spec["rows"]):
            raise Existing301Error(f"reference catalog row mismatch: {path}")
        by_id = {str(row["theorem_id"]): row for row in rows}
        if len(by_id) != len(rows):
            raise Existing301Error(f"duplicate theorem IDs in reference catalog: {path}")
        result[str(family)] = by_id
        consumed.append(_binding(repo_root, path))
    return result


def _context_record(
    repo_root: Path,
    recipe: dict[str, Any],
    source_context_id: str,
    *,
    helper_path: Path,
    pins: RuntimePins,
    consumed: list[FileBinding],
) -> CompileContextRecord:
    raw = cast(dict[str, Any], dict(recipe["source_contexts"])[source_context_id])
    path = _resolve(repo_root, str(raw["path"]))
    if hash_file(path) != raw["sha256"]:
        raise Existing301Error(f"source context hash mismatch: {path}")
    if all(item.path != _binding(repo_root, path).path for item in consumed):
        consumed.append(_binding(repo_root, path))
    context, _source = compile_context_from_source(
        source_context_path=path, helper_path=helper_path, pins=pins
    )
    return CompileContextRecord(
        source_context_id=source_context_id,
        render_compile_context_id=context.compile_context_id,
        project_id=context.project_id,
        project_revision=context.project_revision,
        project_path="/storage/milikic/leanfaith/mathlib4",
        lean_version=context.lean_version,
        import_header=context.import_header,
        namespace_context=context.namespace_context,
        open_context=context.open_context,
        scoped_context=context.scoped_context,
        options=dict(context.options),
        source_context_path=str(path),
        source_context_sha256=str(raw["sha256"]),
        helper_path=str(helper_path),
        helper_sha256=pins.sft2b_helper_hash,
    )


def load_existing_301(
    repo_root: Path,
    *,
    recipe_path: Path,
    helper_path: Path,
    pins: RuntimePins,
    require_frozen_bundle: bool = True,
) -> tuple[tuple[ExistingCandidate, ...], Existing301Receipt]:
    """Recover and validate all 301 inputs without invoking Lean or a model."""

    recipe = _object(recipe_path)
    if recipe.get("schema_version") != "sft2b_existing_301_recipe_v1":
        raise Existing301Error("unsupported existing-301 recipe")
    for raw_path, expected in dict(recipe["data_reuse_bundle"]).items():
        if hash_file(Path(raw_path)) != expected:
            raise Existing301Error(f"DATA-REUSE bundle hash mismatch: {raw_path}")
    manifests = _canonical_manifests(repo_root, recipe)
    if len(manifests) != int(recipe["expected_manifest_count"]):
        raise Existing301Error("canonical manifest count mismatch")
    manifest_bindings = [_binding(repo_root, path) for path in manifests]
    consumed: list[FileBinding] = list(manifest_bindings)
    references = _reference_catalogs(repo_root, recipe, consumed)
    contexts: dict[str, CompileContextRecord] = {}
    recovered: list[ExistingCandidate] = []
    family_counts: Counter[str] = Counter()
    unique_refs: set[str] = set()
    all_unknown = True
    required_files = tuple(str(item) for item in recipe["required_invocation_files"])
    for manifest_path in manifests:
        manifest = _object(manifest_path)
        pair_paths = sorted(manifest_path.parent.glob("invocations/*/unresolved_pairs.jsonl"))
        if len(pair_paths) != int(manifest["admitted_pair_count"]):
            raise Existing301Error(f"manifest/pair count mismatch: {manifest_path}")
        for pair_path in pair_paths:
            invocation = pair_path.parent
            paths = {name: invocation / name for name in required_files}
            if any(not path.is_file() for path in paths.values()):
                raise Existing301Error(f"incomplete invocation: {invocation}")
            consumed.extend(_binding(repo_root, path) for path in paths.values())
            pair = _one_jsonl(paths["unresolved_pairs.jsonl"])
            nl_lean = _object(paths["unresolved_nl_lean.json"])
            candidate_repr = _object(paths["admitted_representation.json"])
            parsed = _object(paths["parsed_candidate.json"])
            variant = _object(paths["admitted_variant.json"])
            theorem = _object(paths["admitted_theorem.json"])
            if (
                nl_lean.get("resolved_label_id") is not None
                or pair.get("resolved_label_id") is not None
                or pair.get("metadata", {}).get("same_claim") is not None
            ):
                all_unknown = False
            if parsed.get("semantic_label") is not None:
                all_unknown = False
            if parsed.get("lean_status") != "valid_with_sorry":
                raise Existing301Error(f"legacy candidate lacks stored elaboration: {invocation}")
            reference_ids = nl_lean.get("reference_theorem_ids")
            if not isinstance(reference_ids, list) or len(reference_ids) != 1:
                raise Existing301Error(f"expected one reference theorem: {invocation}")
            reference_id = str(reference_ids[0])
            family = _family(pair_path)
            reference = references[family].get(reference_id)
            if reference is None:
                raise Existing301Error(f"missing reference {reference_id} in {family} catalog")
            proposition = str(reference["signature_pp"])
            candidate_proposition = str(candidate_repr["signature_pp"])
            if "⋯" in proposition or "[anonymous]" in proposition:
                raise Existing301Error(
                    f"reference proposition contains placeholder: {reference_id}"
                )
            if "⋯" in candidate_proposition or "[anonymous]" in candidate_proposition:
                raise Existing301Error(f"candidate proposition contains placeholder: {invocation}")
            source_context_id = str(candidate_repr["context_id"])
            if source_context_id not in contexts:
                contexts[source_context_id] = _context_record(
                    repo_root,
                    recipe,
                    source_context_id,
                    helper_path=helper_path,
                    pins=pins,
                    consumed=consumed,
                )
            metadata = cast(dict[str, Any], variant["metadata"])
            lineage = FormalizerLineage(
                origin=CandidateOrigin.EXISTING_301,
                provider="legacy_local_hf",
                model_id=str(variant["generator_id"]),
                model_revision=str(variant["generator_id"]).rsplit("@", 1)[-1],
                prompt_sha256=str(metadata["prompt_template_hash"]),
                decoding_sha256=str(variant["generation_config_hash"]),
                seed=int(variant["seed"]),
                upstream_call_id=str(metadata["llm_call_id"]),
                upstream_generation_config_sha256=str(variant["generation_config_hash"]),
            )
            nl_statement = str(nl_lean["nl_statement"])
            source_id = stable_id(
                "sft2b_source",
                {
                    "reference_theorem_id": reference_id,
                    "nl_statement": nl_statement,
                    "source_revision": str(nl_lean["source_revision"]),
                },
            )
            pair_binding = _binding(repo_root, pair_path)
            manifest_binding = _binding(repo_root, manifest_path)
            source = SourceRecord(
                source_id=source_id,
                legacy_pair_id=str(pair["pair_id"]),
                nl_statement=nl_statement,
                reference_theorem_id=reference_id,
                reference_declaration_name=str(
                    reference.get("declaration_full_name") or theorem.get("declaration_full_name")
                ),
                reference_proposition=proposition,
                reference_proposition_sha256=sha256_hex(proposition.encode("utf-8")),
                compile_context=contexts[source_context_id],
                provenance=SourceProvenance(
                    source_family=family,  # type: ignore[arg-type]
                    source_url=str(
                        theorem.get("nl_source_link")
                        or "https://github.com/leanprover-community/mathlib4"
                    ),
                    source_revision=str(nl_lean["source_revision"]),
                    source_path=pair_binding.path,
                    source_file_sha256=pair_binding.sha256,
                    manifest_path=manifest_binding.path,
                    manifest_sha256=manifest_binding.sha256,
                    source_recipe_sha256=str(recipe["data_reuse_tree_sha256"]),
                    license_card_value="Apache-2.0",
                    redistribution_note=(
                        "public Mathlib/docstring lineage; private-first SFT2B release"
                    ),
                    nl_extraction_rule="frozen adjacent/trusted Mathlib docstring record",
                    trusted_reference_basis="frozen elaborated Mathlib reference representation",
                ),
                standalone_nl=True,
                trusted_reference=True,
                training_eligible=False,
            )
            candidate_hash = sha256_hex(candidate_proposition.encode("utf-8"))
            candidate_id = stable_id(
                "sft2b_candidate",
                {
                    "source_id": source_id,
                    "slot": CandidateSlot.SLOT_0,
                    "signature_sha256": candidate_hash,
                    "source_context_id": source_context_id,
                    "lineage": lineage.model_dump(mode="json"),
                },
            )
            candidate = CandidateRecord(
                candidate_id=candidate_id,
                source_id=source_id,
                slot=CandidateSlot.SLOT_0,
                raw_proof_free_signature=candidate_proposition,
                signature_sha256=candidate_hash,
                source_context_id=source_context_id,
                lineage=lineage,
                legacy_candidate_theorem_id=str(candidate_repr["theorem_id"]),
                legacy_pair_id=str(pair["pair_id"]),
            )
            recovered.append(
                ExistingCandidate(
                    source=source,
                    candidate=candidate,
                    pair_path=pair_binding.path,
                    pair_path_sha256=pair_binding.sha256,
                    manifest_path=manifest_binding.path,
                    manifest_sha256=manifest_binding.sha256,
                )
            )
            family_counts[family] += 1
            unique_refs.add(reference_id)
    # Deduplicate shared catalogs/contexts while retaining every invocation file.
    unique_bindings = {item.path: item for item in consumed}
    if len(unique_bindings) != len(consumed):
        consumed = list(unique_bindings.values())
    bundle_hash = _binding_hash(consumed)
    expected_bundle = str(recipe["expected_consumed_bundle_sha256"])
    if require_frozen_bundle and bundle_hash != expected_bundle:
        raise Existing301Error(
            f"consumed bundle hash mismatch: expected {expected_bundle}, got {bundle_hash}"
        )
    if len(recovered) != int(recipe["expected_candidate_count"]):
        raise Existing301Error("existing candidate count mismatch")
    if len(unique_refs) != int(recipe["expected_unique_reference_count"]):
        raise Existing301Error("unique reference count mismatch")
    expected_family_counts = {
        str(key): int(value) for key, value in dict(recipe["expected_family_counts"]).items()
    }
    if dict(family_counts) != expected_family_counts:
        raise Existing301Error("existing candidate family counts mismatch")
    if len(consumed) != int(recipe["expected_consumed_file_count"]):
        raise Existing301Error(
            f"consumed file count mismatch: expected {recipe['expected_consumed_file_count']}, "
            f"got {len(consumed)}"
        )
    if not all_unknown:
        raise Existing301Error("existing 301 includes a semantic label")
    receipt = Existing301Receipt(
        schema_version="sft2b_existing_301_receipt_v1",
        recipe_path=str(recipe_path.relative_to(repo_root)),
        recipe_sha256=hash_file(recipe_path),
        data_reuse_tree_sha256=str(recipe["data_reuse_tree_sha256"]),
        manifests=tuple(sorted(manifest_bindings, key=lambda item: item.path)),
        consumed_files=tuple(sorted(consumed, key=lambda item: item.path)),
        consumed_bundle_sha256=bundle_hash,
        candidate_count=len(recovered),
        unique_reference_count=len(unique_refs),
        family_counts=dict(family_counts),
        all_unknown=True,
        superseded_public_tranches_excluded=True,
    )
    return tuple(sorted(recovered, key=lambda item: item.candidate.legacy_pair_id or "")), receipt


def receipt_bytes(receipt: Existing301Receipt) -> bytes:
    return canonical_json_bytes(receipt.to_dict()) + b"\n"
