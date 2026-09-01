"""Provider-free correction and replay of the SFT2A v5.2 certified sample."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.sft2a.legacy import _atomic_exact
from leanfaith.sft2a.mechanisms import (
    SignatureShape,
    mechanism_histogram,
    plan_structured_mechanism_rotation,
    structured_signature_shape,
)
from leanfaith.sft2a.models import OneRootConfig

CORRECTOR_VERSION = "sft2a_certified_sample_corrector_v5_2_1"
FORBIDDEN_MODEL_GOAL_MARKERS = ("[anonymous]", "⋯", "...")
COMPOSITION_REGRESSION = "Composition.orderEmbOfFin_boundaries"


class CorrectedSampleError(RuntimeError):
    """A frozen certificate, structured goal, or exact replay invariant failed."""


def _object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CorrectedSampleError(f"invalid JSON artifact: {path}") from exc
    if not isinstance(value, dict):
        raise CorrectedSampleError(f"JSON artifact is not an object: {path}")
    return value


def _rows(path: Path) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CorrectedSampleError(f"invalid JSONL line {number}: {path}") from exc
        if not isinstance(value, dict):
            raise CorrectedSampleError(f"non-object JSONL line {number}: {path}")
        result.append(value)
    return result


def _payload_from_cache(cache_path: Path) -> tuple[dict[str, object], dict[str, object]]:
    cache = _object(cache_path)
    raw_path = cache.get("raw_response_path")
    if not isinstance(raw_path, str) or not raw_path:
        raise CorrectedSampleError("certification cache lacks raw response path")
    raw = _object(Path(raw_path))
    response = raw.get("response")
    if not isinstance(response, dict) or not isinstance(response.get("messages"), list):
        raise CorrectedSampleError("certification raw response lacks messages")
    payloads: list[dict[str, object]] = []
    for message in response["messages"]:
        if not isinstance(message, dict):
            continue
        for line in str(message.get("data", "")).splitlines():
            if "LFGOALV1EXPRJSON " not in line:
                continue
            value = json.loads(line.split("LFGOALV1EXPRJSON ", maxsplit=1)[1])
            if isinstance(value, dict):
                payloads.append(value)
    if len(payloads) != 1:
        raise CorrectedSampleError("certification cache does not have exactly one Expr payload")
    payload = payloads[0]
    tree = payload.get("expr_tree")
    if not isinstance(tree, dict):
        raise CorrectedSampleError("certification Expr payload lacks a tree")
    if hash_canonical(tree) != cache.get("closed_expr_hash"):
        raise CorrectedSampleError("certification Expr tree hash differs from cache")
    if payload.get("goal_v1") != cache.get("goal_v1"):
        raise CorrectedSampleError("certification rendered goal differs from raw Expr payload")
    return cache, payload


def certified_shape(certified: Mapping[str, object]) -> tuple[SignatureShape, str]:
    cache_path = certified.get("certification_cache_path", certified.get("cache_path"))
    goal = certified.get("goal_v1")
    closed_hash = certified.get("closed_expr_hash")
    if not all(isinstance(value, str) and value for value in (cache_path, goal, closed_hash)):
        raise CorrectedSampleError("certified reference lacks its cache, goal, or Expr hash")
    assert isinstance(cache_path, str) and isinstance(goal, str)
    cache, payload = _payload_from_cache(Path(cache_path))
    if cache.get("status") != "valid" or cache.get("closed_expr_hash") != closed_hash:
        raise CorrectedSampleError("certified reference differs from terminal cache")
    tree = payload["expr_tree"]
    assert isinstance(tree, dict)
    shape = structured_signature_shape(goal, tree)
    return shape, hash_canonical({"goal_v1": goal, "expr_tree": tree, "shape": asdict(shape)})


def verify_certified_reference_row(row: Mapping[str, object]) -> dict[str, object]:
    """Validate a sample reference without constructing Lean or trusting source signature text."""

    root = row.get("root")
    certified = row.get("certified_reference")
    if not isinstance(root, dict) or not isinstance(certified, dict):
        raise CorrectedSampleError("certified sample row is malformed")
    parsed_root = OneRootConfig.model_validate(root)
    cache_path_value = certified.get("certification_cache_path")
    if not isinstance(cache_path_value, str):
        raise CorrectedSampleError("certified row lacks cache path")
    cache_path = Path(cache_path_value)
    if hash_file(cache_path) != certified.get("certification_cache_sha256"):
        raise CorrectedSampleError("certification cache file hash differs")
    cache, payload = _payload_from_cache(cache_path)
    expected_route = (
        "loaded_constant_type"
        if parsed_root.source in {"mathlib", "physlib", "cslib"}
        else "term_elaborated_proposition"
    )
    if certified.get("route") != expected_route or cache.get("route") != expected_route:
        raise CorrectedSampleError("certified reference used the wrong source route")
    if parsed_root.source in {"mathlib", "physlib", "cslib"}:
        key_payload = cache.get("key_payload")
        identity = key_payload.get("identity") if isinstance(key_payload, dict) else None
        if (
            not isinstance(identity, dict)
            or identity.get("qualified_declaration_name") != parsed_root.declaration_name
            or payload.get("expr_origin") != "loaded_constant_type"
        ):
            raise CorrectedSampleError("library reference is not bound to ConstantInfo.type")
    elif payload.get("expr_origin") != "term_elaborated_proposition":
        raise CorrectedSampleError(
            "compiler-data reference did not use proof-free term elaboration"
        )
    comparisons = {
        "goal_v1": cache.get("goal_v1"),
        "closed_expr_hash": cache.get("closed_expr_hash"),
        "rendered_goal_hash": cache.get("rendered_goal_hash"),
        "sidecar_hash": cache.get("sidecar_hash"),
        "compile_context_id": cache.get("compile_context_id"),
        "certification_cache_key": cache.get("cache_key"),
    }
    for key, observed in comparisons.items():
        if certified.get(key) != observed:
            raise CorrectedSampleError(f"certified reference {key} differs from cache")
    if parsed_root.expected_reference_goal_v1 != certified.get("goal_v1"):
        raise CorrectedSampleError("expected reference goal differs from certified goal")
    shape, structure_hash = certified_shape(certified)
    return {
        "root_id": parsed_root.root_id,
        "source": parsed_root.source,
        "declaration_name": parsed_root.declaration_name,
        "shape": asdict(shape),
        "shape_id": shape.shape_id,
        "structured_goal_hash": structure_hash,
        "cache_hit": True,
        "lean_requests_executed": 0,
        "provider_calls_executed": 0,
    }


def _replacement_row(pool_row: Mapping[str, object], result_path: Path) -> dict[str, object]:
    result = _object(result_path)
    certification = result.get("certification")
    if not isinstance(certification, dict):
        raise CorrectedSampleError("replacement certificate or pool root is malformed")
    goal = certification.get("goal_v1")
    cache_path_value = certification.get("cache_path")
    if not isinstance(goal, str) or not isinstance(cache_path_value, str):
        raise CorrectedSampleError("replacement certificate lacks goal or cache")
    parsed_root = OneRootConfig.model_validate(
        {
            **{
                key: pool_row[key]
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
            "expected_reference_goal_v1": goal,
        }
    )
    cache_path = Path(cache_path_value)
    return {
        "root": parsed_root.model_dump(mode="json"),
        "source_locator": pool_row["source_locator"],
        "source_header": pool_row["source_header"],
        "source_header_sha256": pool_row["source_header_sha256"],
        "compiler_data_theorem_sha256": pool_row.get("compiler_data_theorem_sha256"),
        "domain": pool_row["domain"],
        "shape_id": "pending_structured_shape",
        "certified_reference": {
            "goal_v1": goal,
            "closed_expr_hash": certification["closed_expr_hash"],
            "rendered_goal_hash": certification["rendered_goal_hash"],
            "certification_cache_key": certification["cache_key"],
            "certification_cache_path": str(cache_path),
            "certification_cache_sha256": hash_file(cache_path),
            "sidecar_hash": certification["sidecar_hash"],
            "compile_context_id": certification["compile_context_id"],
            "route": certification["route"],
            "result_path": str(result_path),
            "result_sha256": hash_file(result_path),
        },
        "mechanism_plan": {},
    }


def prepare_corrected_sample(
    *,
    source_output: Path,
    output: Path,
    salt: str,
    maximum_family_fraction_per_polarity: float,
) -> dict[str, object]:
    """Replace placeholder roots and regenerate all dependent artifacts with zero calls."""

    old_sample_path = source_output / "certified_sample.jsonl"
    old_rows = _rows(old_sample_path)
    if len(old_rows) != 100:
        raise CorrectedSampleError("source certified sample is not exactly 100 roots")
    rejected: list[dict[str, object]] = []
    retained: list[dict[str, object]] = []
    for row in old_rows:
        root = row.get("root")
        certified = row.get("certified_reference")
        if not isinstance(root, dict) or not isinstance(certified, dict):
            raise CorrectedSampleError("source certified sample row is malformed")
        goal = certified.get("goal_v1")
        if not isinstance(goal, str):
            raise CorrectedSampleError("source certified goal is missing")
        markers = [marker for marker in FORBIDDEN_MODEL_GOAL_MARKERS if marker in goal]
        if markers:
            rejected.append(
                {
                    "root_id": root.get("root_id"),
                    "declaration_name": root.get("declaration_name"),
                    "markers": markers,
                    "closed_expr_hash": certified.get("closed_expr_hash"),
                    "rendered_goal_hash": certified.get("rendered_goal_hash"),
                }
            )
        else:
            verify_certified_reference_row(row)
            retained.append(row)
    if len(rejected) != 1 or rejected[0].get("declaration_name") != COMPOSITION_REGRESSION:
        raise CorrectedSampleError("placeholder regression set differs from Composition canary")

    existing_ids: set[str] = set()
    existing_exprs: set[str] = set()
    existing_goals: set[str] = set()
    for row in old_rows:
        root_document = row.get("root")
        if not isinstance(root_document, dict):
            raise CorrectedSampleError("source certified root is malformed")
        existing_ids.add(str(root_document["root_id"]))
    for row in retained:
        certified_document = row.get("certified_reference")
        if not isinstance(certified_document, dict):
            raise CorrectedSampleError("retained certified reference is malformed")
        existing_exprs.add(str(certified_document["closed_expr_hash"]))
        existing_goals.add(str(certified_document["rendered_goal_hash"]))
    candidates: list[tuple[str, dict[str, object]]] = []
    for pool_row in _rows(source_output / "initial_pool.jsonl"):
        if pool_row.get("source") != "mathlib" or pool_row.get("root_id") in existing_ids:
            continue
        root_id = str(pool_row.get("root_id"))
        result_path = source_output / "results" / "mathlib" / f"{hash_canonical(root_id)}.json"
        if not result_path.is_file():
            continue
        result = _object(result_path)
        certification = result.get("certification")
        if not isinstance(certification, dict) or certification.get("status") != "valid":
            continue
        goal = certification.get("goal_v1")
        if not isinstance(goal, str) or any(
            marker in goal for marker in FORBIDDEN_MODEL_GOAL_MARKERS
        ):
            continue
        if (
            certification.get("closed_expr_hash") in existing_exprs
            or certification.get("rendered_goal_hash") in existing_goals
        ):
            continue
        candidate = _replacement_row(pool_row, result_path)
        verify_certified_reference_row(candidate)
        candidates.append(
            (hash_canonical({"salt": salt, "replacement_root_id": root_id}), candidate)
        )
    if not candidates:
        raise CorrectedSampleError("no already-certified Mathlib replacement is available")
    _rank, replacement = min(candidates, key=lambda item: item[0])
    rows = [*retained, replacement]
    if Counter(str(row["root"]["source"]) for row in rows) != {  # type: ignore[index]
        "mathlib": 42,
        "physlib": 25,
        "cslib": 17,
        "compiler_data": 16,
    }:
        raise CorrectedSampleError("corrected sample changed the frozen source mix")

    shapes: dict[str, tuple[SignatureShape, str]] = {}
    for row in rows:
        root_id = str(row["root"]["root_id"])  # type: ignore[index]
        shapes[root_id] = certified_shape(row["certified_reference"])  # type: ignore[arg-type]
    rotation = plan_structured_mechanism_rotation(
        [(root_id, shape) for root_id, (shape, _hash) in shapes.items()],
        salt=salt + ":structured",
        maximum_family_fraction_per_polarity=maximum_family_fraction_per_polarity,
    )
    final_rows: list[dict[str, object]] = []
    for source_row in rows:
        row = dict(source_row)
        root_id = str(row["root"]["root_id"])  # type: ignore[index]
        shape, structure_hash = shapes[root_id]
        row["shape_id"] = shape.shape_id
        row["structured_goal"] = {
            "version": "sft2a_structured_certified_goal_v5_2_1",
            "shape": asdict(shape),
            "structure_hash": structure_hash,
        }
        row["mechanism_plan"] = {
            slot: assignment.to_dict() for slot, assignment in sorted(rotation[root_id].items())
        }
        final_rows.append(row)
    final_rows.sort(
        key=lambda row: (
            str(row["root"]["compile_context"]["project_id"]),  # type: ignore[index]
            str(row["root"]["root_id"]),  # type: ignore[index]
        )
    )
    sample_bytes = b"".join(canonical_json_bytes(row) + b"\n" for row in final_rows)
    _atomic_exact(output / "certified_sample.jsonl", sample_bytes)
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in final_rows:
        grouped[str(row["root"]["compile_context"]["project_id"])].append(row)  # type: ignore[index]
    shards: list[dict[str, object]] = []
    for project_id in sorted(grouped):
        for start in range(0, len(grouped[project_id]), 10):
            shard_rows = grouped[project_id][start : start + 10]
            path = (
                output / "certified_shards" / project_id / f"{project_id}-{start // 10:03d}.jsonl"
            )
            _atomic_exact(
                path,
                b"".join(canonical_json_bytes(row) + b"\n" for row in shard_rows),
            )
            shards.append(
                {
                    "project_id": project_id,
                    "path": str(path.relative_to(output)),
                    "roots": len(shard_rows),
                    "sha256": hash_file(path),
                }
            )
    one_binder = sorted(
        root_id for root_id, (shape, _hash) in shapes.items() if shape.binder_count == 1
    )
    impossible = [
        {"root_id": root_id, "slot_id": slot, "family": assignment.family}
        for root_id in one_binder
        for slot, assignment in rotation[root_id].items()
        if assignment.applicability == "two_binders"
    ]
    if impossible:
        raise CorrectedSampleError("one-binder roots retain impossible two-binder mechanisms")
    regression = {
        "version": "leanfaith_sft2a_certified_goal_regressions_v5_2",
        "placeholder_regression": rejected[0],
        "one_binder_root_ids": one_binder,
        "one_binder_impossible_assignments": impossible,
        "lambda_arrow_not_equality_or_order": True,
        "nested_function_arrow_not_premise": True,
    }
    _atomic_exact(
        output / "structured_goal_regressions.json",
        canonical_json_bytes(regression) + b"\n",
    )
    manifest: dict[str, object] = {
        "version": CORRECTOR_VERSION,
        "source_certification_root": str(source_output),
        "source_certification_manifest_sha256": hash_file(
            source_output / "certification_manifest.json"
        ),
        "source_sample_sha256": hash_file(old_sample_path),
        "root_count": len(final_rows),
        "source_mix": dict(
            sorted(Counter(str(row["root"]["source"]) for row in final_rows).items())  # type: ignore[index]
        ),
        "rejected_regressions": rejected,
        "replacement_root_id": replacement["root"]["root_id"],  # type: ignore[index]
        "replacement_declaration_name": replacement["root"]["declaration_name"],  # type: ignore[index]
        "sample_sha256": hash_file(output / "certified_sample.jsonl"),
        "shards": shards,
        "structured_regressions_sha256": hash_file(output / "structured_goal_regressions.json"),
        "mechanism_plan_histogram": mechanism_histogram(rotation),
        "one_binder_roots": one_binder,
        "cache_hits": 100,
        "lean_requests_executed": 0,
        "provider_calls_executed": 0,
        "legacy_rejudge_authorized": False,
        "publication_authorized": False,
        "scale_10k_authorized": False,
        "scale_50k_authorized": False,
    }
    _atomic_exact(output / "corrected_sample_manifest.json", canonical_json_bytes(manifest) + b"\n")
    return manifest


def verify_corrected_sample_replay(output: Path) -> dict[str, object]:
    """Replay all 100 cache terminals without constructing a provider or Lean backend."""

    manifest = _object(output / "corrected_sample_manifest.json")
    sample_path = output / "certified_sample.jsonl"
    rows = _rows(sample_path)
    if len(rows) != 100 or hash_file(sample_path) != manifest.get("sample_sha256"):
        raise CorrectedSampleError("corrected sample count or hash differs")
    before = {
        str(path.relative_to(output)): hash_file(path)
        for path in sorted(output.rglob("*"))
        if path.is_file()
        and path.name not in {"corrected_replay_receipt.json", "global_100_preflight_receipt.json"}
    }
    details = [verify_certified_reference_row(row) for row in rows]
    after = {
        str(path.relative_to(output)): hash_file(path)
        for path in sorted(output.rglob("*"))
        if path.is_file()
        and path.name not in {"corrected_replay_receipt.json", "global_100_preflight_receipt.json"}
    }
    if before != after:
        raise CorrectedSampleError("corrected replay changed durable artifacts")
    receipt: dict[str, object] = {
        "version": "leanfaith_sft2a_corrected_sample_replay_v5_2",
        "sample_sha256": manifest["sample_sha256"],
        "corrected_sample_manifest_sha256": hash_file(output / "corrected_sample_manifest.json"),
        "roots_verified": len(details),
        "cache_hits": len(details),
        "lean_requests_executed": 0,
        "provider_calls_executed": 0,
        "durable_artifact_hashes_preserved": True,
        "durable_tree_hash": hash_canonical(before),
    }
    _atomic_exact(output / "corrected_replay_receipt.json", canonical_json_bytes(receipt) + b"\n")
    return receipt


def verify_corrected_global_preflight(output: Path) -> dict[str, object]:
    replay = verify_corrected_sample_replay(output)
    rows = _rows(output / "certified_sample.jsonl")
    if replay.get("cache_hits") != 100:
        raise CorrectedSampleError("corrected global certificate is not 100/100")
    receipt: dict[str, object] = {
        "version": "leanfaith_sft2a_corrected_global_reference_preflight_v5_2",
        "sample_sha256": hash_file(output / "certified_sample.jsonl"),
        "corrected_sample_manifest_sha256": hash_file(output / "corrected_sample_manifest.json"),
        "corrected_replay_receipt_sha256": hash_file(output / "corrected_replay_receipt.json"),
        "structured_regressions_sha256": hash_file(output / "structured_goal_regressions.json"),
        "certified_roots": len(rows),
        "cache_hits": 100,
        "global_certificate": "100/100",
        "lean_requests_executed": 0,
        "provider_calls_executed": 0,
        "provider_construction_allowed_by_this_receipt": False,
    }
    _atomic_exact(
        output / "global_100_preflight_receipt.json",
        canonical_json_bytes(receipt) + b"\n",
    )
    return receipt


__all__ = [
    "COMPOSITION_REGRESSION",
    "CORRECTOR_VERSION",
    "CorrectedSampleError",
    "certified_shape",
    "prepare_corrected_sample",
    "verify_certified_reference_row",
    "verify_corrected_global_preflight",
    "verify_corrected_sample_replay",
]
