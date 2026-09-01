"""Hash-pinned recovery of the one new audited ReForm smoke source."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from leanfaith.collect2.postprocess import GoldenBlocklist
from leanfaith.config.hashing import hash_file, sha256_hex
from leanfaith.representations.views import normalize_headless, signature_near_dup_hash
from leanfaith.sft2b.lean import compile_context_from_source
from leanfaith.sft2b.pins import RuntimePins
from leanfaith.sft2b.reuse import load_existing_301
from leanfaith.sft2b.schemas import (
    CompileContextRecord,
    SourceProvenance,
    SourceRecord,
    stable_id,
)


class NewSourceError(RuntimeError):
    """Raised when the selected smoke source or its prior audit drifts."""


@dataclass(frozen=True, slots=True)
class NewSourceReceipt:
    schema_version: str
    config_sha256: str
    problem_id: str
    theorem_id: str
    catalog_hashes: dict[str, str]
    source_file_sha256: str
    absent_from_existing_301: bool
    audit_checks: dict[str, bool]
    golden_blocklist_sha256: str | None = None
    reference_near_dup_hash: str | None = None
    golden_checks: dict[str, bool] | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "config_sha256": self.config_sha256,
            "problem_id": self.problem_id,
            "theorem_id": self.theorem_id,
            "catalog_hashes": dict(sorted(self.catalog_hashes.items())),
            "source_file_sha256": self.source_file_sha256,
            "absent_from_existing_301": self.absent_from_existing_301,
            "audit_checks": dict(sorted(self.audit_checks.items())),
            "golden_blocklist_sha256": self.golden_blocklist_sha256,
            "reference_near_dup_hash": self.reference_near_dup_hash,
            "golden_checks": (
                dict(sorted(self.golden_checks.items())) if self.golden_checks is not None else None
            ),
        }


def _object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise NewSourceError(f"expected JSON object: {path}")
    return cast(dict[str, Any], value)


def _one(path: Path, key: str, value: str) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise NewSourceError(f"non-object catalog row: {path}:{number}")
            typed = cast(dict[str, Any], row)
            if typed.get(key) == value:
                matches.append(typed)
    if len(matches) != 1:
        raise NewSourceError(f"expected one {key}={value} row in {path}, got {len(matches)}")
    return matches[0]


def load_new_source(
    repo_root: Path,
    *,
    config_path: Path,
    helper_path: Path,
    pins: RuntimePins,
) -> tuple[SourceRecord, NewSourceReceipt]:
    """Replay the accepted prior audit and build one strict source record."""

    config = _object(config_path)
    schema_version = config.get("schema_version")
    if schema_version not in {"sft2b_new_source_smoke_v1", "sft2b_new_source_smoke_v2"}:
        raise NewSourceError("unsupported new-source smoke config")
    if config.get("external_transmission") is not False:
        raise NewSourceError("new-source smoke must remain local-model-only")
    catalogs: dict[str, Path] = {}
    catalog_hashes: dict[str, str] = {}
    for name, raw_spec in cast(dict[str, dict[str, str]], config["catalogs"]).items():
        path = repo_root / raw_spec["path"]
        catalog_hash = hash_file(path)
        if catalog_hash != raw_spec["sha256"]:
            raise NewSourceError(f"{name} catalog hash mismatch")
        catalogs[name] = path
        catalog_hashes[name] = catalog_hash
    problem_id = str(config["problem_id"])
    theorem_id = str(config["reference_theorem_id"])
    problem = _one(catalogs["problem_pool"], "problem_id", problem_id)
    representation = _one(catalogs["reference_representations"], "theorem_id", theorem_id)
    theorem = _one(catalogs["reference_theorems"], "theorem_id", theorem_id)
    audit = _one(catalogs["record_audits"], "problem_id", problem_id)
    if problem.get("reference_theorem_ids") != [theorem_id]:
        raise NewSourceError("problem/reference join drifted")
    expected_values = {
        "nl_statement": problem.get("nl_statement"),
        "declaration_full_name": theorem.get("declaration_full_name"),
        "reference_proposition": representation.get("signature_pp"),
        "source_revision": theorem.get("source_revision"),
        "source_context_id": representation.get("context_id"),
    }
    for key, observed in expected_values.items():
        if config.get(key) != observed:
            raise NewSourceError(f"selected-source {key} drifted")
    required = cast(dict[str, object], config["required_audit"])
    raw_screens = audit.get("registry_screens")
    if not isinstance(raw_screens, dict):
        raise NewSourceError("selected source lacks registry screens")
    screens = cast(dict[str, Any], raw_screens)
    audit_checks = {
        "curation_decision": audit.get("curation_decision") == "standalone_sufficient",
        "no_sorry_alias_check_valid": audit.get("no_sorry_alias_check_valid") is True,
        "source_pair_present": audit.get("source_pair_present") is True,
        "all_three_registry_screens_clear": screens.get("all_three_screens_clear") is True,
        "temporal_strictly_postdates_latest_checkpoint": (
            audit.get("temporal_strictly_postdates_latest_checkpoint") is True
        ),
        "release_eligible": problem.get("release_eligible") is True,
        "trusted_nl": problem.get("nl_trust") == "trusted",
    }
    expected_required: dict[str, object] = {
        **audit_checks,
        "curation_decision": "standalone_sufficient",
    }
    if required != expected_required:
        raise NewSourceError("selected source no longer satisfies its frozen audit gate")
    golden_blocklist_sha256: str | None = None
    reference_near_dup_hash: str | None = None
    golden_checks: dict[str, bool] | None = None
    if schema_version == "sft2b_new_source_smoke_v2":
        raw_blocklist = cast(dict[str, str], config["golden_blocklist"])
        blocklist_path = repo_root / raw_blocklist["path"]
        golden_blocklist_sha256 = hash_file(blocklist_path)
        if golden_blocklist_sha256 != raw_blocklist["sha256"]:
            raise NewSourceError("golden blocklist hash mismatch")
        blocklist = GoldenBlocklist.load(blocklist_path)
        raw_declaration = theorem.get("proof_stripped_declaration")
        if not isinstance(raw_declaration, str):
            raise NewSourceError("reference theorem lacks a proof-stripped declaration")
        headless = normalize_headless(raw_declaration)
        if headless is None:
            raise NewSourceError("reference theorem cannot be normalized for golden screening")
        reference_near_dup_hash = signature_near_dup_hash(headless)
        if reference_near_dup_hash != config.get("reference_near_dup_hash"):
            raise NewSourceError("reference near-duplicate hash drifted")
        golden_checks = {
            "problem_identity_clear": not blocklist.problem_is_blocked(problem_id),
            "reference_signature_clear": (reference_near_dup_hash not in blocklist.near_dup_hashes),
        }
        if not all(golden_checks.values()):
            raise NewSourceError("selected source intersects the golden evaluation blocklist")
    source_path = Path(str(config["source_path"]))
    source_hash = hash_file(source_path)
    if source_hash != config["source_file_sha256"] or source_hash != audit["source_file_sha256"]:
        raise NewSourceError("pinned Mathlib source file drifted")
    context_path = repo_root / str(config["context_path"])
    if hash_file(context_path) != config["context_sha256"]:
        raise NewSourceError("new-source compile context drifted")
    context, _ = compile_context_from_source(
        source_context_path=context_path,
        helper_path=helper_path,
        pins=pins,
    )
    source_context_id = str(config["source_context_id"])
    context_record = CompileContextRecord(
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
        source_context_path=str(context_path),
        source_context_sha256=str(config["context_sha256"]),
        helper_path=str(helper_path),
        helper_sha256=pins.sft2b_helper_hash,
    )
    existing_recipe = repo_root / str(config["existing_301_recipe_path"])
    existing, _ = load_existing_301(
        repo_root,
        recipe_path=existing_recipe,
        helper_path=helper_path,
        pins=pins,
    )
    existing_references = {item.source.reference_theorem_id for item in existing}
    absent = theorem_id not in existing_references
    if not absent:
        raise NewSourceError("selected source is not new relative to the existing 301")
    nl_statement = str(config["nl_statement"])
    revision = str(config["source_revision"])
    source_id = stable_id(
        "sft2b_source",
        {
            "reference_theorem_id": theorem_id,
            "nl_statement": nl_statement,
            "source_revision": revision,
        },
    )
    proposition = str(config["reference_proposition"])
    source = SourceRecord(
        source_id=source_id,
        nl_statement=nl_statement,
        reference_theorem_id=theorem_id,
        reference_declaration_name=str(config["declaration_full_name"]),
        reference_proposition=proposition,
        reference_proposition_sha256=sha256_hex(proposition.encode("utf-8")),
        compile_context=context_record,
        provenance=SourceProvenance(
            source_family="new_audited",
            source_url=str(config["source_url"]),
            source_revision=revision,
            source_path=str(source_path),
            source_file_sha256=source_hash,
            manifest_path=str(catalogs["problem_pool"]),
            manifest_sha256=catalog_hashes["problem_pool"],
            source_recipe_sha256=hash_file(config_path),
            license_card_value=str(config["source_license"]),
            redistribution_note=str(config["redistribution_note"]),
            nl_extraction_rule="pinned adjacent Mathlib docstring with prior standalone audit",
            trusted_reference_basis=(
                "pinned elaborated Mathlib reference plus successful no-sorry alias check"
            ),
            benchmark_exact_hit=False,
            benchmark_near_hit=False,
        ),
        standalone_nl=True,
        trusted_reference=True,
        training_eligible=False,
    )
    receipt = NewSourceReceipt(
        schema_version="sft2b_new_source_receipt_v1",
        config_sha256=hash_file(config_path),
        problem_id=problem_id,
        theorem_id=theorem_id,
        catalog_hashes=catalog_hashes,
        source_file_sha256=source_hash,
        absent_from_existing_301=absent,
        audit_checks=audit_checks,
        golden_blocklist_sha256=golden_blocklist_sha256,
        reference_near_dup_hash=reference_near_dup_hash,
        golden_checks=golden_checks,
    )
    return source, receipt
