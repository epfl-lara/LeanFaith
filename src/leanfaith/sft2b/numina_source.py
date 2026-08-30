"""Strict recovery and audit of one permitted Numina smoke source."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pyarrow.parquet as pq  # type: ignore[import-untyped]
import yaml

from leanfaith.collect2.postprocess import GoldenBlocklist
from leanfaith.config.hashing import hash_canonical, hash_file, sha256_hex
from leanfaith.representations.views import collapse_lean_whitespace, signature_near_dup_hash
from leanfaith.sft2b.lean import compile_context_from_source
from leanfaith.sft2b.pins import RuntimePins
from leanfaith.sft2b.reuse import load_existing_301
from leanfaith.sft2b.schemas import (
    CompileContextRecord,
    SourceProvenance,
    SourceRecord,
    stable_id,
)

_COMMENT = re.compile(r"/-\s*(.*?)\s*-/", flags=re.DOTALL)


class NuminaSourceError(RuntimeError):
    """Raised when the pinned dataset row or its audit contract drifts."""


@dataclass(frozen=True, slots=True)
class NuminaSourceReceipt:
    schema_version: str
    config_sha256: str
    dataset_revision: str
    parquet_sha256: str
    row_sha256: str
    question_sha256: str
    lean_code_sha256: str
    policy_sha256: str
    golden_blocklist_sha256: str
    reference_near_dup_hash: str
    audit_checks: dict[str, bool]
    absent_from_existing_301: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "config_sha256": self.config_sha256,
            "dataset_revision": self.dataset_revision,
            "parquet_sha256": self.parquet_sha256,
            "row_sha256": self.row_sha256,
            "question_sha256": self.question_sha256,
            "lean_code_sha256": self.lean_code_sha256,
            "policy_sha256": self.policy_sha256,
            "golden_blocklist_sha256": self.golden_blocklist_sha256,
            "reference_near_dup_hash": self.reference_near_dup_hash,
            "audit_checks": dict(sorted(self.audit_checks.items())),
            "absent_from_existing_301": self.absent_from_existing_301,
        }


def _object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise NuminaSourceError(f"expected JSON object: {path}")
    return cast(dict[str, Any], value)


def _theorem_result(source: str, declaration_name: str) -> str:
    pattern = re.compile(
        rf"\btheorem\s+{re.escape(declaration_name)}\s*:\s*(.*?)\s*:=\s*by\b",
        flags=re.DOTALL,
    )
    matches = pattern.findall(source)
    if len(matches) != 1:
        raise NuminaSourceError(
            f"expected one theorem {declaration_name!r}, observed {len(matches)}"
        )
    return collapse_lean_whitespace(matches[0])


def load_numina_source(
    repo_root: Path,
    *,
    config_path: Path,
    helper_path: Path,
    pins: RuntimePins,
) -> tuple[SourceRecord, NuminaSourceReceipt]:
    """Recover one exact row and replay every quality/authorization screen."""

    config = _object(config_path)
    if config.get("schema_version") != "sft2b_numina_source_smoke_v1":
        raise NuminaSourceError("unsupported Numina source config")
    if (
        config.get("policy_version") != "source_use_v2"
        or config.get("external_transmission") is not True
    ):
        raise NuminaSourceError("Numina source must explicitly use source_use_v2")
    raw_policy = cast(dict[str, str], config["policy"])
    policy_path = repo_root / raw_policy["path"]
    policy_hash = hash_file(policy_path)
    if policy_hash != raw_policy["sha256"]:
        raise NuminaSourceError("source-use policy hash mismatch")
    policy = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    if not isinstance(policy, dict):
        raise NuminaSourceError("source-use policy is not a mapping")
    scope = policy.get("scope")
    if (
        policy.get("policy_version") != "source_use_v2"
        or policy.get("external_model_processing") is not True
        or not isinstance(scope, dict)
        or scope.get("namespace") != "formalmathatepfl/*"
    ):
        raise NuminaSourceError("source-use policy no longer authorizes this source")
    raw_dataset = cast(dict[str, object], config["dataset"])
    revision = str(raw_dataset["revision"])
    snapshot = Path(str(raw_dataset["snapshot_path"]))
    if snapshot.name != revision or not snapshot.is_dir():
        raise NuminaSourceError("dataset snapshot/revision mismatch")
    readme_path = snapshot / str(raw_dataset["readme_path"])
    parquet_path = snapshot / str(raw_dataset["train_path"])
    if hash_file(readme_path) != raw_dataset["readme_sha256"]:
        raise NuminaSourceError("dataset README hash mismatch")
    parquet_hash = hash_file(parquet_path)
    if parquet_hash != raw_dataset["train_sha256"]:
        raise NuminaSourceError("dataset train shard hash mismatch")
    uuid = str(config["row_uuid"])
    table = pq.read_table(parquet_path, filters=[("uuid", "=", uuid)])
    if table.num_rows != 1:
        raise NuminaSourceError(f"expected one UUID row, observed {table.num_rows}")
    row = {name: table[name][0].as_py() for name in table.column_names}
    row_hash = hash_canonical(row)
    if row_hash != config["row_sha256"]:
        raise NuminaSourceError("selected row hash mismatch")
    question = str(row["question"])
    lean_code = str(row["lean_code"])
    question_hash = sha256_hex(question.encode("utf-8"))
    lean_hash = sha256_hex(lean_code.encode("utf-8"))
    if question_hash != config["question_sha256"] or lean_hash != config["lean_code_sha256"]:
        raise NuminaSourceError("selected question or Lean code hash mismatch")
    declaration_name = str(config["declaration_name"])
    proposition = str(config["reference_proposition"])
    comments = _COMMENT.findall(question)
    nl_statement = str(config["nl_statement"])
    question_prop = _theorem_result(question, declaration_name)
    lean_prop = _theorem_result(lean_code, declaration_name)
    expected_audit = cast(dict[str, bool], config["required_audit"])
    audit_checks = {
        "row_valid": row.get("valid") is True,
        "not_proof_repair": row.get("proof_repair") is False,
        "data_source_matches": row.get("data_source") == config["data_source"],
        "one_nl_comment": len(comments) == 1 and comments[0].strip() == nl_statement,
        "question_reference_matches": question_prop == proposition,
        "lean_reference_matches": lean_prop == proposition,
        "standalone_nl": bool(nl_statement.strip()),
    }
    if audit_checks != expected_audit or not all(audit_checks.values()):
        raise NuminaSourceError("selected Numina row failed its frozen quality audit")
    raw_blocklist = cast(dict[str, str], config["golden_blocklist"])
    blocklist_path = repo_root / raw_blocklist["path"]
    blocklist_hash = hash_file(blocklist_path)
    if blocklist_hash != raw_blocklist["sha256"]:
        raise NuminaSourceError("golden blocklist hash mismatch")
    blocklist = GoldenBlocklist.load(blocklist_path)
    reference_near_hash = signature_near_dup_hash(f": {proposition}")
    if reference_near_hash != config["reference_near_dup_hash"]:
        raise NuminaSourceError("reference near-duplicate hash drifted")
    problem_identity = str(config["problem_identity"])
    if blocklist.problem_is_blocked(problem_identity) or (
        reference_near_hash in blocklist.near_dup_hashes
    ):
        raise NuminaSourceError("selected Numina row intersects the golden blocklist")
    context_path = repo_root / str(config["context_path"])
    if hash_file(context_path) != config["context_sha256"]:
        raise NuminaSourceError("Numina compile context drifted")
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
    existing_hashes = {item.source.reference_proposition_sha256 for item in existing}
    proposition_hash = sha256_hex(proposition.encode("utf-8"))
    absent = proposition_hash not in existing_hashes
    if not absent:
        raise NuminaSourceError("selected Numina reference duplicates the existing 301")
    theorem_id = f"numina:{uuid}:{declaration_name}"
    source_id = stable_id(
        "sft2b_source",
        {
            "reference_theorem_id": theorem_id,
            "nl_statement": nl_statement,
            "source_revision": revision,
        },
    )
    source = SourceRecord(
        source_id=source_id,
        nl_statement=nl_statement,
        reference_theorem_id=theorem_id,
        reference_declaration_name=declaration_name,
        reference_proposition=proposition,
        reference_proposition_sha256=proposition_hash,
        compile_context=context_record,
        provenance=SourceProvenance(
            source_family="new_audited",
            source_url=str(config["source_url"]),
            source_revision=revision,
            source_path=str(parquet_path),
            source_file_sha256=parquet_hash,
            manifest_path=str(readme_path),
            manifest_sha256=str(raw_dataset["readme_sha256"]),
            source_recipe_sha256=hash_file(config_path),
            license_card_value="not_declared_in_pinned_readme",
            redistribution_note=str(config["redistribution_note"]),
            nl_extraction_rule="the unique pinned Lean block comment in the exact UUID row",
            trusted_reference_basis=(
                "exact question/lean_code signature agreement plus valid=true; reference "
                "proposition is independently elaborated by SFT2B"
            ),
            benchmark_exact_hit=False,
            benchmark_near_hit=False,
        ),
        standalone_nl=True,
        trusted_reference=True,
        training_eligible=False,
    )
    receipt = NuminaSourceReceipt(
        schema_version="sft2b_numina_source_receipt_v1",
        config_sha256=hash_file(config_path),
        dataset_revision=revision,
        parquet_sha256=parquet_hash,
        row_sha256=row_hash,
        question_sha256=question_hash,
        lean_code_sha256=lean_hash,
        policy_sha256=policy_hash,
        golden_blocklist_sha256=blocklist_hash,
        reference_near_dup_hash=reference_near_hash,
        audit_checks=audit_checks,
        absent_from_existing_301=absent,
    )
    return source, receipt
