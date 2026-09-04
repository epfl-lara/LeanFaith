"""Historical certificate squares and Wave 4 shared-edge closure orbits.

For a certified negative ``P ≁ C``, each Wave 4 group carries preserving
chains from ``P`` to ``P'`` and ``C`` to ``C'``, plus an exact replay of the
negative mechanism at the terminal endpoints.  Every logical group contains:

1. preserving reference edge ``P' ↔ P``;
2. preserving candidate edge ``C ↔ C'``;
3. certified base negative ``C ≁ P``;
4. certified negative-last edge ``P' ≁ C'``.

Several groups may reference one physically stored base-negative row, so release
compaction validates a four-edge logical index instead of assuming four physical
rows per ancestry root.  The legacy fixed-square commands remain additive and
unchanged for their historical releases.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from collections import deque
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from pydantic import ValidationError

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.config.loading import ConfigError, LoadedConfig, load_yaml_mapping
from leanfaith.config.paths import find_repo_root
from leanfaith.host_resources import Reservation
from leanfaith.lean.protocol import LeanRequest, LeanStatus
from leanfaith.representations.goal_v1 import (
    ClosedExprInput,
    ClosedExprSidecar,
    ClosedExprSourceMaterial,
    GoalV1Error,
    render_closed_expr_in_session,
)
from leanfaith.schemas.ids import PAIR_PREFIX, VARIANT_PREFIX, make_id
from leanfaith.sft1.sprint import engine as engine_module
from leanfaith.sft1.sprint.engine import cacheable_status, lean_string_literal, parse_evidence_lines
from leanfaith.sft1.sprint.inventory import load_inventory
from leanfaith.sft1.sprint.orbit import (
    EDGE_ROLES,
    OrbitError,
    OrbitPolicy,
    cap_negative_operation_share,
    policy_from_config,
)
from leanfaith.sft1.sprint.provenance import derive_provenance
from leanfaith.sft1.sprint.runner import (
    RunPaths,
    SprintConfig,
    SprintRunner,
    _count_by,
    _git,
    canonical_surface,
    load_sprint_config,
    read_retained,
    utc_now,
)
from leanfaith.sft1.sprint.screens import (
    GoldBlocklist,
    deduplicate,
    render_hash,
    residue_violation,
    unordered_pair_key,
)
from leanfaith.sft1.sprint.store import SemanticCache, read_json_object, write_atomic

SQUARE_OPERATION = "SQUARE_N25_SYMMETRY_V1"
SQUARE_CACHE_KIND = "square_root"
SQUARE_CACHE_SCHEMA_LEGACY = 2  # semantic version + pins; records could be overwritten
SQUARE_CACHE_SCHEMA = 3  # + operation revision, engine source hash, compile-context identity
# square operation id -> (negative operation it closes, census file, family prefix)
SQUARE_OPERATIONS: dict[str, dict[str, str]] = {
    "SQUARE_N25_SYMMETRY_V1": {
        "negative": "N25_TOGGLE_EQ_NE_PROOF_V1",
        "census": "square_n25.json",
        "family": "square",
    },
    "SQUARE_N25_BINDER_V1": {
        "negative": "N25_TOGGLE_EQ_NE_PROOF_V1",
        "census": "square_n25.json",
        "family": "square_n25",
    },
    "SQUARE_N32_BINDER_V1": {
        "negative": "N32_SWAP_ROLE_ORDER_PROOF_V1",
        "census": "square_n32.json",
        "family": "square_n32",
    },
    # catalog N19 whole-claim negation over every inventory theorem; curriculum-only
    "SQUARE_N19_CURRICULUM_V1": {
        "negative": "N19_WHOLE_CLAIM_NEGATION_V1",
        "census": "square_n19.json",
        "family": "square_n19",
        # cache revision 1: records written before the P15 witness dispatch existed are stale
        "cache_revision": "1",
    },
    "SQUARE_WAVE2_N26_V1": {
        "negative": "N26_INCREMENT_BOUND_PROOF_V1",
        "census": "square_wave2_n26.json",
        "family": "wave2_n26",
    },
    "SQUARE_WAVE2_N32_V1": {
        "negative": "N32_SWAP_ROLE_ORDER_PROOF_V1",
        "census": "square_wave2_n32.json",
        "family": "wave2_n32",
    },
    "SQUARE_WAVE2_N25_V1": {
        "negative": "N25_TOGGLE_EQ_NE_PROOF_V1",
        "census": "square_wave2_n25.json",
        "family": "wave2_n25",
    },
    "SQUARE_WAVE2_N31_V1": {
        "negative": "N31_DROP_REQUIRED_GUARD_PROOF_V1",
        "census": "square_wave2_n31.json",
        "family": "wave2_n31",
    },
    "ORBIT_WAVE4_N31_V1": {
        "negative": "N31_DROP_REQUIRED_GUARD_PROOF_V1",
        "census": "orbit_wave4_n31.json",
        "family": "wave4_n31",
        "cache_revision": "1",
    },
    "ORBIT_WAVE4_N26_V1": {
        "negative": "N26_INCREMENT_BOUND_PROOF_V1",
        "census": "orbit_wave4_n26.json",
        "family": "wave4_n26",
        "cache_revision": "1",
    },
    "ORBIT_WAVE4_N32_V1": {
        "negative": "N32_SWAP_ROLE_ORDER_PROOF_V1",
        "census": "orbit_wave4_n32.json",
        "family": "wave4_n32",
        "cache_revision": "1",
    },
    "ORBIT_WAVE4_N30_V1": {
        "negative": "N30_ADD_UNJUSTIFIED_UNIQUENESS_PROOF_V1",
        "census": "orbit_wave4_n30.json",
        "family": "wave4_n30",
        "cache_revision": "1",
    },
    "ORBIT_WAVE4_N29_V1": {
        "negative": "N29_SWAP_WITNESS_DEPENDENCY_PROOF_V1",
        "census": "orbit_wave4_n29.json",
        "family": "wave4_n29",
        "cache_revision": "1",
    },
    "ORBIT_WAVE4_N25_V1": {
        "negative": "N25_TOGGLE_EQ_NE_PROOF_V1",
        "census": "orbit_wave4_n25.json",
        "family": "wave4_n25",
        "cache_revision": "1",
    },
}

WAVE4_OPERATIONS = frozenset(
    operation_id for operation_id in SQUARE_OPERATIONS if operation_id.startswith("ORBIT_WAVE4_")
)
WAVE4_CACHE_SCHEMA = 2
WAVE4_CACHE_KIND = "wave4_orbit_root"
WAVE4_RELEASE_SCHEMA = 4
WAVE4_RELEASE_ID_PREFIX = "wave4_release:"
WAVE4_PROJECTS = frozenset({"mathlib", "physlib", "cslib"})
WAVE4_COMPOSITION_GATE_SCHEMA = 1
DEFAULT_WAVE4_CONFIG = Path("configs/transformations/sft1_value_first_v1/wave4_v1.yaml")


def operation_cache_revision(operation_id: str) -> int:
    """Per-operation cache revision; 0 keeps the historical key composition unchanged."""
    return int(SQUARE_OPERATIONS[operation_id].get("cache_revision", "0"))


def square_cache_key(
    *,
    operation_id: str,
    name: str,
    engine_semantic_version: str,
    project_revision: str,
    lean_version: str,
    import_options_fingerprint: str,
    revision: int,
    schema: int = SQUARE_CACHE_SCHEMA,
    engine_source_sha256: str | None = None,
    compile_context_id: str | None = None,
) -> str:
    """Cache identity of one square-root record.

    Schema 2 (legacy) binds the engine semantic version and the project pins only, so a
    later run with a different engine text could overwrite a record. Schema 3 also binds
    the operation revision, the engine source hash, and the compile-context identity, so
    a key names exactly one deterministic computation.
    """
    if schema == SQUARE_CACHE_SCHEMA_LEGACY:
        identity: dict[str, Any] = {
            "kind": SQUARE_CACHE_KIND,
            "cache_schema": SQUARE_CACHE_SCHEMA_LEGACY,
            "operation_id": operation_id,
            "name": name,
            "engine_semantic_version": engine_semantic_version,
            "project_revision": project_revision,
            "lean_version": lean_version,
            "import_options_fingerprint": import_options_fingerprint,
        }
        if revision > 0:
            identity["operation_revision"] = revision
        return hash_canonical(identity)
    if schema != SQUARE_CACHE_SCHEMA:
        raise SquareError(f"unknown square cache schema {schema}")
    if not engine_source_sha256 or not compile_context_id:
        raise SquareError("schema 3 cache keys need the engine source hash and compile context")
    return hash_canonical(
        {
            "kind": SQUARE_CACHE_KIND,
            "cache_schema": SQUARE_CACHE_SCHEMA,
            "operation_id": operation_id,
            "operation_revision": revision,
            "name": name,
            "engine_source_sha256": engine_source_sha256,
            "compile_context_id": compile_context_id,
            "engine_semantic_version": engine_semantic_version,
            "project_revision": project_revision,
            "lean_version": lean_version,
            "import_options_fingerprint": import_options_fingerprint,
        }
    )


INVENTORY_NEGATIVES = {"N19_WHOLE_CLAIM_NEGATION_V1"}
TRANSFORM_SHORT = {
    "P18_SYMMETRIZE_EQUALITY_V1": "eq",
    "P_NE_SYMMETRIZE_V1": "ne",
    "P14_SWAP_INDEPENDENT_DATA_BINDERS_V1": "p14",
    "P23_CURRY_PROP_PAIR_V1": "p23",
    "P15_SWAP_IFF_SIDES_V1": "p15",
    "P21_BETA_REDUCE_V1": "p21_beta",
    "P21_ZETA_REDUCE_V1": "p21_zeta",
    "P32_ADD_ASSOC_LOCAL_V1": "p32_assoc",
    "P32_ADD_COMM_LOCAL_V1": "p32_comm",
    "P35_SET_INTER_MEMBERSHIP_V1": "p35_inter",
}


def census_path_for(staging: Path, operation_id: str) -> Path:
    return staging / "targets" / SQUARE_OPERATIONS[operation_id]["census"]


SQUARE_SALT = "sft1_sprint_core_v3_square"
ROW_SCHEMA = "sft_core_v1"
ENDPOINT_ROLE = {"p": "candidate", "c": "reference", "p_prime": "reference", "c_prime": "candidate"}
ENDPOINT_ORIGIN = {
    "p": "loaded_constant_type",
    "c": "sft1_transformed_expr",
    "p_prime": "sft1_transformed_expr",
    "c_prime": "sft1_transformed_expr",
}
# (row kind, label, reference endpoint, candidate endpoint, evidence key)
ENDPOINT_TRUTH: dict[str, str] = {
    "p": "proved",  # the loaded Mathlib theorem
    "p_prime": "proved",  # transported along the checked P-iff-P'
    "c": "refuted",  # the certified N25 negative of `P`
    "c_prime": "refuted",  # derived from `¬C` along the checked C-iff-C'
}
ROW_KINDS: tuple[tuple[str, bool, str, str, str], ...] = (
    ("p_prime_iff_p", True, "p_prime", "p", "p_prime_iff_p"),
    ("c_iff_c_prime", True, "c", "c_prime", "c_iff_c_prime"),
    ("not_iff_c_p", False, "c", "p", "not_iff_c_p"),
    ("not_iff_p_prime_c_prime", False, "p_prime", "c_prime", "not_iff_p_prime_c_prime"),
)
ROW_LABEL = {kind: label for kind, label, _, _, _ in ROW_KINDS}
WAVE4_ROW_KINDS: tuple[tuple[str, bool, str, str, str], ...] = (
    ("preserving_reference", True, "p_prime", "p", "p_composite_iff"),
    ("preserving_candidate", True, "c", "c_prime", "c_composite_iff"),
    ("negative_base", False, "c", "p", "not_iff_c_p"),
    ("negative_last", False, "p_prime", "c_prime", "not_iff_p_prime_c_prime"),
)
WAVE4_ROW_LABEL = {kind: label for kind, label, _, _, _ in WAVE4_ROW_KINDS}
SOURCE_RUNS = ("tenk", "v2_ne", "v2_lt")


class SquareError(RuntimeError):
    """Fail-closed square error."""


@dataclass(frozen=True, slots=True)
class LoadedWave4Config:
    """Validated sprint runtime plus the executable Wave 4 orbit policy."""

    runtime: LoadedConfig[SprintConfig]
    policy: OrbitPolicy
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ValidatedWave4Variant:
    """One complete typed closure returned by the Lean Wave 4 enumerator."""

    index: int
    depth: int
    selection_hash: str
    content_hash: str
    reference_chain_hash: str
    candidate_chain_hash: str
    reference_site_hash: str
    candidate_site_hash: str
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Wave4VariantDescriptor:
    """Cheap operation/site identity used before full certificate validation.

    The descriptor deliberately excludes proof objects.  It lets the runner apply
    the stable max-five policy before parsing and frozen-rendering certificates, while
    the selected variants still undergo the complete fail-closed validation below.
    """

    index: int
    depth: int
    selection_hash: str
    content_hash: str
    reference_chain_hash: str
    candidate_chain_hash: str
    reference_site_hash: str
    candidate_site_hash: str
    base_edge_hash: str
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ValidatedWave4Root:
    """The complete terminal for one root, before the stable max-five selection."""

    root: str
    operation_id: str
    negative_operation: str
    selection_root_id: str
    variants: tuple[ValidatedWave4Variant, ...]
    enumeration_hash: str


def _wave4_effective_document(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve a strict project-runtime overlay without copying the Wave 4 policy.

    Physlib and CSLib need different pinned runner contexts, but they must use the
    byte-identical composition/release policy from ``wave4_v1.yaml``.  An overlay
    may therefore contain only the common document identity, one sibling policy
    filename, and a complete replacement ``runtime`` mapping.  The returned
    semantic source retains both files so neither can change without changing the
    executable config hash.
    """

    overlay = load_yaml_mapping(path)
    policy_config = overlay.get("policy_config")
    if policy_config is None:
        return overlay, {"policy": overlay, "runtime_overlay": None}
    if not isinstance(policy_config, str) or Path(policy_config).name != policy_config:
        raise ConfigError("Wave 4 policy_config must be one sibling filename")
    allowed = {"schema_version", "wave_id", "policy_config", "runtime"}
    extra = set(overlay).difference(allowed)
    if extra:
        raise ConfigError(f"Wave 4 runtime overlay has unsupported fields: {sorted(extra)}")
    base_path = path.parent / policy_config
    base = load_yaml_mapping(base_path)
    if "policy_config" in base:
        raise ConfigError("Wave 4 policy_config overlays may not be chained")
    for identity_field in ("schema_version", "wave_id"):
        if overlay.get(identity_field) != base.get(identity_field):
            raise ConfigError(f"Wave 4 runtime overlay changes {identity_field}")
    runtime = overlay.get("runtime")
    if not isinstance(runtime, dict):
        raise ConfigError(f"wave4 config {path} is missing a runtime mapping")
    effective = dict(base)
    effective["runtime"] = runtime
    return effective, {
        "policy_path": str(base_path.resolve()),
        "policy": base,
        "runtime_overlay_path": str(path.resolve()),
        "runtime_overlay": overlay,
    }


def load_wave4_config(repo_root: Path, config_path: Path | None = None) -> LoadedWave4Config:
    """Load the additive Wave 4 policy and its embedded existing-runner config.

    The runner config is intentionally nested: the top-level Wave 4 document carries
    composition, selection, release, and safety policy that ``SprintConfig`` must not
    silently ignore.  The semantic hash binds the entire strict YAML mapping as well as
    the validated runtime defaults and parsed orbit policy.
    """

    path = config_path or repo_root / DEFAULT_WAVE4_CONFIG
    raw, semantic_source = _wave4_effective_document(path)
    runtime_raw = raw.get("runtime")
    if not isinstance(runtime_raw, dict):
        raise ConfigError(f"wave4 config {path} is missing a runtime mapping")
    try:
        runtime_config = SprintConfig.model_validate(runtime_raw)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(loc) for loc in error['loc'])}: {error['msg']}"
            for error in exc.errors(include_input=False, include_url=False)
        )
        raise ConfigError(f"invalid wave4 runtime config {path}: {details}") from exc
    composition = _wave4_mapping(raw.get("composition"), "wave4.config.composition")
    if composition.get("exact_negative_last_operation_replay_required") is not True:
        raise ConfigError("Wave 4 config must require exact negative-last operation replay")
    closure_storage = _wave4_mapping(raw.get("closure_storage"), "wave4.config.closure_storage")
    if closure_storage.get("negative_last_replay_required") is not True:
        raise ConfigError("Wave 4 closure storage must retain the negative-last replay")
    required_negative_fields = {
        "boundary",
        "separator",
        "witnesses",
        "witness_checks",
        "enumeration",
    }
    configured_negative_fields = {
        str(value)
        for value in _wave4_sequence(
            closure_storage.get("negative_family_evidence_required"),
            "wave4.config.closure_storage.negative_family_evidence_required",
        )
    }
    if configured_negative_fields != required_negative_fields:
        raise ConfigError("Wave 4 closure storage changes required negative-family evidence")
    policy = policy_from_config(raw)
    semantic_hash = hash_canonical(
        {
            "kind": "sft1_wave4_executable_config_v1",
            "source_documents": semantic_source,
            "effective": raw,
            "runtime": runtime_config.model_dump(mode="json"),
            "orbit_policy": policy.payload(),
        }
    )
    runtime = LoadedConfig(
        config=runtime_config,
        path=path,
        raw=dict(runtime_raw),
        config_hash=semantic_hash,
    )
    return LoadedWave4Config(runtime=runtime, policy=policy, raw=raw)


def wave4_cache_key(
    *,
    operation_id: str,
    name: str,
    policy_hash: str,
    maximum_depth: int,
    engine_source_sha256: str,
    compile_context_id: str,
    engine_semantic_version: str,
    project_revision: str,
    lean_version: str,
    import_options_fingerprint: str,
    revision: int,
) -> str:
    """Content identity of one complete Wave 4 root enumeration."""

    if operation_id not in WAVE4_OPERATIONS:
        raise OrbitError(f"{operation_id!r} is not a Wave 4 orbit operation")
    if not 1 <= maximum_depth <= 3:
        raise OrbitError("Wave 4 cache depth must be between one and three")
    return hash_canonical(
        {
            "kind": WAVE4_CACHE_KIND,
            "cache_schema": WAVE4_CACHE_SCHEMA,
            "operation_id": operation_id,
            "operation_revision": revision,
            "root": name,
            "policy_hash": policy_hash,
            "maximum_depth": maximum_depth,
            "engine_source_sha256": engine_source_sha256,
            "compile_context_id": compile_context_id,
            "engine_semantic_version": engine_semantic_version,
            "project_revision": project_revision,
            "lean_version": lean_version,
            "import_options_fingerprint": import_options_fingerprint,
        }
    )


def _wave4_mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise OrbitError(f"{field} must be a string-keyed mapping")
    return cast(Mapping[str, Any], value)


def _wave4_sequence(value: object, field: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        raise OrbitError(f"{field} must be a sequence")
    return value


def _wave4_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise OrbitError(f"{field} must be nonempty text")
    return value


def _wave4_nat(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise OrbitError(f"{field} must be a nonnegative integer")
    return value


def _wave4_u64(value: object, field: str) -> str:
    text = _wave4_text(value, field)
    if not text.isascii() or not text.isdecimal() or int(text) >= 2**64:
        raise OrbitError(f"{field} must be a decimal UInt64")
    return text


def _wave4_sha256(value: object, field: str) -> str:
    text = _wave4_text(value, field)
    if re.fullmatch(r"[0-9a-f]{64}", text) is None:
        raise OrbitError(f"{field} must be a lowercase SHA-256 digest")
    return text


def _wave4_checked(value: object, field: str) -> Mapping[str, Any]:
    check = _wave4_mapping(value, field)
    if check.get("meta_checked") is not True or check.get("kernel_checked") is not True:
        raise OrbitError(f"{field} is not Meta- and kernel-checked")
    _wave4_text(check.get("kernel_level_instantiation"), f"{field}.kernel_level_instantiation")
    _wave4_u64(check.get("proof_expr_hash_u64"), f"{field}.proof_expr_hash_u64")
    return check


def _wave4_site(value: object, field: str) -> dict[str, Any]:
    site = _wave4_mapping(value, field)
    _wave4_text(site.get("kind"), f"{field}.kind")
    _wave4_nat(site.get("index"), f"{field}.index")
    _wave4_text(site.get("detail"), f"{field}.detail")
    _wave4_nat(site.get("guard_variable_index"), f"{field}.guard_variable_index")
    bound = site.get("bound_variable_index")
    if bound is not None:
        _wave4_nat(bound, f"{field}.bound_variable_index")
    if type(site.get("literal")) is not int:
        raise OrbitError(f"{field}.literal must be an integer")
    path = _wave4_sequence(site.get("path"), f"{field}.path")
    for index, step in enumerate(path):
        _wave4_nat(step, f"{field}.path[{index}]")
    return dict(site)


def _wave4_grounding(value: object, field: str) -> Mapping[str, Any]:
    grounding = _wave4_mapping(value, field)
    assignments = _wave4_sequence(grounding.get("assignment"), f"{field}.assignment")
    binder_count = _wave4_nat(grounding.get("binder_count"), f"{field}.binder_count")
    if binder_count != len(assignments):
        raise OrbitError(f"{field} binder count does not match assignments")
    for index, raw_assignment in enumerate(assignments):
        assignment = _wave4_mapping(raw_assignment, f"{field}.assignment[{index}]")
        if _wave4_nat(assignment.get("index"), f"{field}.assignment[{index}].index") != index:
            raise OrbitError(f"{field} assignment indices are not canonical")
        _wave4_text(assignment.get("description"), f"{field}.assignment[{index}].description")
        _wave4_u64(
            assignment.get("value_expr_hash_u64"),
            f"{field}.assignment[{index}].value_expr_hash_u64",
        )
        _wave4_u64(
            assignment.get("value_type_hash_u64"),
            f"{field}.assignment[{index}].value_type_hash_u64",
        )
        _wave4_text(assignment.get("source_kind"), f"{field}.assignment[{index}].source_kind")
    _wave4_nat(grounding.get("tactic_calls"), f"{field}.tactic_calls")
    _wave4_text(grounding.get("universe_instantiation"), f"{field}.universe_instantiation")
    return grounding


def _wave4_disjoint_after_first(sites: Sequence[Mapping[str, Any]], field: str) -> None:
    """Replay the engine's conservative, transport-free multi-hop site policy."""

    prior: list[Mapping[str, Any]] = []
    for index, site in enumerate(sites):
        if index:
            path = cast(Sequence[int], site["path"])
            if not path or path[0] != 3:
                raise OrbitError(f"{field}[{index}] is not a root-coordinate binder site")
            current = cast(int, site["index"])
            for old in prior:
                old_path = cast(Sequence[int], old["path"])
                if not old_path or old_path[0] != 3 or current + 1 >= cast(int, old["index"]):
                    raise OrbitError(f"{field}[{index}] lacks a disjoint root coordinate")
        prior.append(site)


def _wave4_negative_certificate(
    value: object, *, negative_operation: str, field: str
) -> Mapping[str, Any]:
    """Validate and retain the complete family-specific negative certificate.

    Wave 4 may transport the refutation through preserving equivalences, but it must
    not flatten the originating negative evidence to a generic ``Not candidate``
    check.  These fields are the same evidence contract as a direct Wave 3 row.
    """

    certificate = _wave4_mapping(value, field)
    _wave4_text(certificate.get("kind"), f"{field}.kind")
    _wave4_checked(certificate.get("check"), f"{field}.check")
    _wave4_grounding(certificate.get("grounding"), f"{field}.grounding")
    required = {"boundary", "separator", "witnesses", "witness_checks", "enumeration"}
    missing = sorted(required.difference(certificate))
    if missing:
        raise OrbitError(f"{field} drops family-specific evidence fields: {missing}")

    boundary = certificate.get("boundary")
    if boundary is not None and type(boundary) is not int:
        raise OrbitError(f"{field}.boundary must be an integer or null")
    separator = certificate.get("separator")
    if separator is not None:
        separator_record = _wave4_mapping(separator, f"{field}.separator")
        _wave4_text(separator_record.get("kind"), f"{field}.separator.kind")
        _wave4_checked(separator_record.get("check"), f"{field}.separator.check")
    witnesses = _wave4_sequence(certificate.get("witnesses"), f"{field}.witnesses")
    for index, witness in enumerate(witnesses):
        _wave4_text(witness, f"{field}.witnesses[{index}]")
    witness_checks = _wave4_sequence(certificate.get("witness_checks"), f"{field}.witness_checks")
    for index, check in enumerate(witness_checks):
        _wave4_checked(check, f"{field}.witness_checks[{index}]")
    enumeration = certificate.get("enumeration")
    if enumeration is not None:
        _wave4_text(enumeration, f"{field}.enumeration")

    if (
        negative_operation
        in {
            "N31_DROP_REQUIRED_GUARD_PROOF_V1",
            "N26_INCREMENT_BOUND_PROOF_V1",
        }
        and separator is None
    ):
        raise OrbitError(f"{field} drops the checked guard separator for {negative_operation}")
    if negative_operation == "N26_INCREMENT_BOUND_PROOF_V1" and not witnesses:
        raise OrbitError(f"{field} drops the exact N26 boundary witness")
    if negative_operation == "N30_ADD_UNJUSTIFIED_UNIQUENESS_PROOF_V1":
        if len(witnesses) != 2 or len(witness_checks) < 3 or separator is None:
            raise OrbitError(f"{field} lacks two checked distinct N30 witnesses")
        if not isinstance(enumeration, str) or not enumeration:
            raise OrbitError(f"{field} drops the finite N30 enumeration kind")
    if negative_operation == "N29_SWAP_WITNESS_DEPENDENCY_PROOF_V1":
        if not witnesses or len(witness_checks) != len(witnesses) or separator is None:
            raise OrbitError(f"{field} lacks the complete checked N29 counterexample matrix")
        if not isinstance(enumeration, str) or "complete_matrix" not in enumeration:
            raise OrbitError(f"{field} drops the complete N29 enumeration kind")
    return certificate


def _describe_wave4_variant(
    raw_variant: object,
    *,
    root: str,
    operation_id: str,
    negative_operation: str,
    policy: OrbitPolicy,
    maximum_depth: int,
    selection_root_id: str,
) -> Wave4VariantDescriptor:
    """Validate proof-free operation/site structure for selection.

    The first protocol phase supplies ``hops`` directly.  Historical complete
    payloads and the selected second-phase report carry the same structural
    fields under ``evidence.hops``.  Only these normalized fields participate in
    descriptor selection; proof objects and rendered text are deliberately not
    touched here.
    """

    variant = _wave4_mapping(raw_variant, "wave4.variant")
    index = _wave4_nat(variant.get("index"), "wave4.variant.index")
    depth = _wave4_nat(variant.get("depth"), "wave4.variant.depth")
    if not 1 <= depth <= maximum_depth:
        raise OrbitError("Wave 4 variant depth is outside the configured bound")
    alpha = {
        endpoint: _wave4_u64(
            variant.get(f"{endpoint}_alpha_hash"), f"wave4.variant.{endpoint}_alpha_hash"
        )
        for endpoint in ("p", "c", "p_prime", "c_prime")
    }
    if len(set(alpha.values())) != 4:
        raise OrbitError("Wave 4 descriptor endpoints are not pairwise distinct")

    evidence_value = variant.get("evidence")
    if evidence_value is None:
        hops = _wave4_sequence(variant.get("hops"), "wave4.variant.hops")
        negative_site_value = variant.get("negative_site")
    else:
        evidence = _wave4_mapping(evidence_value, "wave4.variant.evidence")
        if evidence.get("negative_operation") != negative_operation:
            raise OrbitError("Wave 4 evidence changes the configured negative operation")
        _wave4_text(evidence.get("direction"), "wave4.variant.evidence.direction")
        hops = _wave4_sequence(evidence.get("hops"), "wave4.variant.evidence.hops")
        negative_site_value = variant.get("negative_site")
        if negative_site_value is None:
            replay = _wave4_mapping(
                evidence.get("negative_last_replay"),
                "wave4.variant.evidence.negative_last_replay",
            )
            negative_site_value = replay.get("site")
    negative_site = _wave4_site(negative_site_value, "wave4.variant.negative_site")
    if len(hops) != depth:
        raise OrbitError("Wave 4 hop count does not match variant depth")

    registry = policy.operation_map()
    mechanisms: set[str] = set()
    superclasses: set[str] = set()
    inverse_tokens: set[str] = set()
    p_sites: list[dict[str, Any]] = []
    c_sites: list[dict[str, Any]] = []
    p_operations: list[str] = []
    c_operations: list[str] = []
    p_values = [alpha["p"]]
    c_values = [alpha["c"]]
    normalized_hops: list[dict[str, Any]] = []
    for hop_index, raw_hop in enumerate(hops):
        field = f"wave4.variant.evidence.hops[{hop_index}]"
        hop = _wave4_mapping(raw_hop, field)
        p_operation = _wave4_text(hop.get("p_operation"), f"{field}.p_operation")
        c_operation = _wave4_text(hop.get("c_operation"), f"{field}.c_operation")
        p_spec = registry.get(p_operation)
        c_spec = registry.get(c_operation)
        if p_spec is None or c_spec is None:
            raise OrbitError("Wave 4 hop uses an operation absent from the policy")
        if hop.get("mechanism") != p_spec.mechanism:
            raise OrbitError(f"{field} mechanism metadata disagrees with the policy")
        if hop.get("superclass") != p_spec.superclass:
            raise OrbitError(f"{field} superclass metadata disagrees with the policy")
        if hop.get("inverse_token") != p_spec.inverse_token:
            raise OrbitError(f"{field} inverse metadata disagrees with the policy")
        if c_spec.superclass != p_spec.superclass:
            raise OrbitError(f"{field} changes mechanism superclass across closure sides")
        if p_operation != c_operation and p_spec.superclass != "relation_symmetry":
            raise OrbitError(f"{field} pairs distinct operations outside relation symmetry")
        if p_spec.mechanism in mechanisms:
            raise OrbitError("Wave 4 chain repeats a preserving mechanism")
        if p_spec.superclass in superclasses:
            raise OrbitError("Wave 4 chain repeats a preserving superclass")
        if p_spec.inverse_token in inverse_tokens:
            raise OrbitError("Wave 4 chain repeats an inverse token")
        mechanisms.add(p_spec.mechanism)
        superclasses.add(p_spec.superclass)
        inverse_tokens.add(p_spec.inverse_token)

        p_site = _wave4_site(hop.get("p_site"), f"{field}.p_site")
        c_site = _wave4_site(hop.get("c_site"), f"{field}.c_site")
        if (p_site["index"], p_site["path"]) != (c_site["index"], c_site["path"]):
            raise OrbitError(f"{field} does not pair the same exact site coordinate")
        p_input = _wave4_u64(hop.get("p_input_alpha_hash"), f"{field}.p_input_alpha_hash")
        c_input = _wave4_u64(hop.get("c_input_alpha_hash"), f"{field}.c_input_alpha_hash")
        p_output = _wave4_u64(hop.get("p_output_alpha_hash"), f"{field}.p_output_alpha_hash")
        c_output = _wave4_u64(hop.get("c_output_alpha_hash"), f"{field}.c_output_alpha_hash")
        if p_input != p_values[-1] or c_input != c_values[-1]:
            raise OrbitError("Wave 4 preserving-hop alpha hashes do not link")
        if p_output in p_values or c_output in c_values:
            raise OrbitError("Wave 4 preserving chain repeats an expression hash")
        if hop.get("site_transport") != "disjoint_root_coordinates":
            raise OrbitError(f"{field} lacks the exact disjoint-site certificate tag")
        p_operations.append(p_operation)
        c_operations.append(c_operation)
        p_sites.append(p_site)
        c_sites.append(c_site)
        p_values.append(p_output)
        c_values.append(c_output)
        normalized_hops.append(
            {
                "p_operation": p_operation,
                "c_operation": c_operation,
                "mechanism": p_spec.mechanism,
                "superclass": p_spec.superclass,
                "inverse_token": p_spec.inverse_token,
                "p_site": p_site,
                "c_site": c_site,
                "p_input_alpha_hash": p_input,
                "c_input_alpha_hash": c_input,
                "p_output_alpha_hash": p_output,
                "c_output_alpha_hash": c_output,
                "site_transport": "disjoint_root_coordinates",
            }
        )
    if p_values[-1] != alpha["p_prime"] or c_values[-1] != alpha["c_prime"]:
        raise OrbitError("Wave 4 chain endpoint alpha hashes do not match the closure")
    _wave4_disjoint_after_first(p_sites, "wave4.variant.p_sites")
    _wave4_disjoint_after_first(c_sites, "wave4.variant.c_sites")

    reference_sites = [hash_canonical(site) for site in p_sites]
    candidate_sites = [hash_canonical(site) for site in c_sites]
    selection_payload = {
        "root_id": selection_root_id,
        "orbit_operation": operation_id,
        "negative_operation": negative_operation,
        "reference_operation_chain": p_operations,
        "reference_site_hashes": reference_sites,
        "candidate_operation_chain": c_operations,
        "candidate_site_hashes": candidate_sites,
    }
    normalized = {
        "index": index,
        "depth": depth,
        **{f"{endpoint}_alpha_hash": value for endpoint, value in alpha.items()},
        "negative_site": negative_site,
        "hops": normalized_hops,
    }
    base_edge_hash = hash_canonical(
        {
            "kind": "sft1_wave4_base_edge_descriptor_v1",
            "root_id": selection_root_id,
            "operation_id": operation_id,
            "negative_operation": negative_operation,
            "p_alpha_hash": alpha["p"],
            "c_alpha_hash": alpha["c"],
            "negative_site": negative_site,
        }
    )
    return Wave4VariantDescriptor(
        index=index,
        depth=depth,
        selection_hash=hash_canonical(
            {"kind": "sft1_wave4_selection_identity_v1", **selection_payload}
        ),
        content_hash=hash_canonical(
            {
                "kind": "sft1_wave4_descriptor_v1",
                **selection_payload,
                "descriptor": normalized,
            }
        ),
        reference_chain_hash=hash_canonical(
            {
                "kind": "sft1_wave4_reference_chain_v1",
                "operations": p_operations,
                "sites": reference_sites,
                "hops": normalized_hops,
            }
        ),
        candidate_chain_hash=hash_canonical(
            {
                "kind": "sft1_wave4_candidate_chain_v1",
                "operations": c_operations,
                "sites": candidate_sites,
                "hops": normalized_hops,
            }
        ),
        reference_site_hash=hash_canonical(reference_sites),
        candidate_site_hash=hash_canonical(candidate_sites),
        base_edge_hash=base_edge_hash,
        raw=normalized,
    )


def _validate_wave4_variant(
    descriptor: Wave4VariantDescriptor,
    raw_variant: object,
    *,
    root: str,
    operation_id: str,
    negative_operation: str,
    policy: OrbitPolicy,
    maximum_depth: int,
    selection_root_id: str,
) -> ValidatedWave4Variant:
    """Fully validate one preselected descriptor and every retained proof object."""

    # Re-describe the selected report so it cannot be paired with a different
    # first-phase operation/site identity.
    replayed = _describe_wave4_variant(
        raw_variant,
        root=root,
        operation_id=operation_id,
        negative_operation=negative_operation,
        policy=policy,
        maximum_depth=maximum_depth,
        selection_root_id=selection_root_id,
    )
    identity_fields = (
        "index",
        "depth",
        "selection_hash",
        "content_hash",
        "reference_chain_hash",
        "candidate_chain_hash",
        "reference_site_hash",
        "candidate_site_hash",
        "base_edge_hash",
    )
    if any(getattr(replayed, field) != getattr(descriptor, field) for field in identity_fields):
        raise OrbitError("Wave 4 preselection descriptor changed before full validation")
    variant = _wave4_mapping(raw_variant, "wave4.selected.variant")
    goals = _wave4_mapping(variant.get("goals"), "wave4.variant.goals")
    canonical_goals: dict[str, str] = {}
    for endpoint in ("p", "c", "p_prime", "c_prime"):
        text = _wave4_text(goals.get(endpoint), f"wave4.variant.goals.{endpoint}")
        canonical, violation = canonical_surface(text)
        if canonical is None:
            raise OrbitError(f"wave4.variant.goals.{endpoint}:{violation}")
        if residue := residue_violation(canonical):
            raise OrbitError(f"wave4.variant.goals.{endpoint}:{residue}")
        canonical_goals[endpoint] = canonical
    if len(set(canonical_goals.values())) != 4:
        raise OrbitError("Wave 4 closure endpoints are not pairwise distinct")

    evidence = _wave4_mapping(variant.get("evidence"), "wave4.variant.evidence")
    hops = _wave4_sequence(evidence.get("hops"), "wave4.variant.evidence.hops")
    for hop_index, raw_hop in enumerate(hops):
        field = f"wave4.variant.evidence.hops[{hop_index}]"
        hop = _wave4_mapping(raw_hop, field)
        _wave4_checked(hop.get("p_direct_iff"), f"{field}.p_direct_iff")
        _wave4_checked(hop.get("c_direct_iff"), f"{field}.c_direct_iff")
    _wave4_checked(evidence.get("p_composite_iff"), "wave4.variant.evidence.p_composite_iff")
    _wave4_checked(evidence.get("c_composite_iff"), "wave4.variant.evidence.c_composite_iff")
    source = _wave4_mapping(evidence.get("source_proof"), "wave4.variant.evidence.source_proof")
    if source.get("kind") != "loaded_environment_constant" or source.get("constant") != root:
        raise OrbitError("Wave 4 source proof is not the requested loaded constant")
    _wave4_u64(
        source.get("value_expr_hash_u64"),
        "wave4.variant.evidence.source_proof.value_expr_hash_u64",
    )
    _wave4_checked(evidence.get("source_proof_check"), "wave4.variant.evidence.source_proof_check")
    _wave4_negative_certificate(
        evidence.get("base_candidate_refutation"),
        negative_operation=negative_operation,
        field="wave4.variant.evidence.base_candidate_refutation",
    )
    for key in (
        "p_prime_transported_proof",
        "c_prime_refutation",
        "not_iff_c_p",
        "not_iff_p_prime_c_prime",
    ):
        _wave4_checked(evidence.get(key), f"wave4.variant.evidence.{key}")

    alpha = {
        endpoint: _wave4_u64(
            variant.get(f"{endpoint}_alpha_hash"), f"wave4.variant.{endpoint}_alpha_hash"
        )
        for endpoint in ("p", "c", "p_prime", "c_prime")
    }
    negative_last = _wave4_mapping(
        evidence.get("negative_last_replay"), "wave4.variant.evidence.negative_last_replay"
    )
    if negative_last.get("operation_id") != negative_operation:
        raise OrbitError("Wave 4 negative-last replay changes the negative operation")
    if negative_last.get("reference_alpha_hash") != alpha["p_prime"]:
        raise OrbitError("Wave 4 negative-last replay does not start at p_prime")
    if negative_last.get("candidate_alpha_hash") != alpha["c_prime"]:
        raise OrbitError("Wave 4 negative-last replay does not produce c_prime")
    for exact_field in (
        "reference_expr_equal",
        "candidate_expr_equal",
        "reference_replay_exact",
        "candidate_replay_exact",
    ):
        if negative_last.get(exact_field) is not True:
            raise OrbitError(f"Wave 4 negative-last replay lacks exact {exact_field}")
    _wave4_site(negative_last.get("site"), "wave4.variant.evidence.negative_last_replay.site")
    _wave4_checked(
        negative_last.get("refutation"),
        "wave4.variant.evidence.negative_last_replay.refutation",
    )
    _wave4_negative_certificate(
        negative_last.get("certificate"),
        negative_operation=negative_operation,
        field="wave4.variant.evidence.negative_last_replay.certificate",
    )

    closure = _wave4_mapping(evidence.get("closure"), "wave4.variant.evidence.closure")
    if closure.get("exact_typed") is not True:
        raise OrbitError("Wave 4 closure is not exact and typed")
    if closure.get("site_policy") != "disjoint_only_no_transport_inference":
        raise OrbitError("Wave 4 closure claims an unsupported site-transport policy")
    if _wave4_nat(closure.get("depth"), "wave4.variant.evidence.closure.depth") != descriptor.depth:
        raise OrbitError("Wave 4 closure depth disagrees with its hop chain")
    certified_raw = dict(variant)
    return ValidatedWave4Variant(
        index=descriptor.index,
        depth=descriptor.depth,
        selection_hash=descriptor.selection_hash,
        content_hash=hash_canonical(
            {
                "kind": "sft1_wave4_certified_variant_v2",
                "descriptor_hash": descriptor.content_hash,
                "variant": certified_raw,
            }
        ),
        reference_chain_hash=descriptor.reference_chain_hash,
        candidate_chain_hash=descriptor.candidate_chain_hash,
        reference_site_hash=descriptor.reference_site_hash,
        candidate_site_hash=descriptor.candidate_site_hash,
        raw=certified_raw,
    )


def validate_wave4_root_payload(
    payload: Mapping[str, Any],
    *,
    operation_id: str,
    policy: OrbitPolicy,
    maximum_depth: int,
    expected_root: str | None = None,
    selected_descriptors: Sequence[Wave4VariantDescriptor] | None = None,
    selection_root_id: str | None = None,
) -> ValidatedWave4Root:
    """Validate a root terminal and the bounded descriptors selected for release.

    Every enumerated operation/site descriptor is checked before selection.  Only the
    stable selected descriptors need their (larger) proof records parsed here.  Passing
    no explicit selection retains the historical full-validation behaviour for callers
    that need to audit every enumerated certificate.
    """

    root, negative_operation, descriptors = _describe_wave4_root_payload(
        payload,
        operation_id=operation_id,
        policy=policy,
        maximum_depth=maximum_depth,
        expected_root=expected_root,
        selection_root_id=selection_root_id,
    )
    if payload.get("kind") == "wave4_descriptor_root":
        raise OrbitError("Wave 4 descriptor terminal has no selected certificates")
    variants_raw = _wave4_sequence(payload.get("variants"), "wave4.variants")
    raw_by_index: dict[int, Mapping[str, Any]] = {}
    raw_indices: list[int] = []
    for raw_variant in variants_raw:
        variant = _wave4_mapping(raw_variant, "wave4.variant")
        index = _wave4_nat(variant.get("index"), "wave4.variant.index")
        if index in raw_by_index:
            raise OrbitError("Wave 4 selected report repeats a descriptor index")
        raw_by_index[index] = variant
        raw_indices.append(index)

    reported_indices = payload.get("selected_descriptor_indices")
    if reported_indices is not None:
        exact_reported = [
            _wave4_nat(value, "wave4.selected_descriptor_indices")
            for value in _wave4_sequence(reported_indices, "wave4.selected_descriptor_indices")
        ]
        if exact_reported != raw_indices:
            raise OrbitError("Wave 4 selected report index order is not exact")
    reported_count = payload.get("selected_variant_count")
    if reported_count is not None and _wave4_nat(
        reported_count, "wave4.selected_variant_count"
    ) != len(variants_raw):
        raise OrbitError("Wave 4 selected report count is incomplete")

    by_index = {descriptor.index: descriptor for descriptor in descriptors}
    if selected_descriptors is None:
        chosen = tuple(by_index[index] for index in raw_indices if index in by_index)
        if len(chosen) != len(raw_indices):
            raise OrbitError("Wave 4 certified variant is absent from the enumeration")
    else:
        chosen_list: list[Wave4VariantDescriptor] = []
        for descriptor in selected_descriptors:
            if by_index.get(descriptor.index) != descriptor:
                raise OrbitError("Wave 4 selected descriptor is absent from the root terminal")
            chosen_list.append(descriptor)
        chosen = tuple(chosen_list)
    selected_only_payload = "descriptors" in payload or reported_indices is not None
    if (
        not chosen
        or len(chosen) > policy.maximum_variants_per_root
        or (selected_only_payload and [descriptor.index for descriptor in chosen] != raw_indices)
    ):
        raise OrbitError("Wave 4 selected descriptor set/order is outside the configured bound")
    variants = tuple(
        _validate_wave4_variant(
            descriptor,
            raw_by_index[descriptor.index],
            root=root,
            operation_id=operation_id,
            negative_operation=negative_operation,
            policy=policy,
            maximum_depth=maximum_depth,
            selection_root_id=selection_root_id or root,
        )
        for descriptor in chosen
    )
    root_identity = selection_root_id or root
    enumeration_hash = hash_canonical(
        {
            "kind": "sft1_wave4_complete_root_enumeration_v2",
            "root": root,
            "root_id": root_identity,
            "operation_id": operation_id,
            "policy_hash": policy.policy_hash,
            "maximum_depth": maximum_depth,
            "descriptors": [descriptor.content_hash for descriptor in descriptors],
        }
    )
    recorded_hash = payload.get("enumeration_hash")
    if recorded_hash is not None and recorded_hash != enumeration_hash:
        raise OrbitError("Wave 4 recorded enumeration hash differs")
    return ValidatedWave4Root(
        root=root,
        operation_id=operation_id,
        negative_operation=negative_operation,
        selection_root_id=root_identity,
        variants=variants,
        enumeration_hash=enumeration_hash,
    )


def _describe_wave4_root_payload(
    payload: Mapping[str, Any],
    *,
    operation_id: str,
    policy: OrbitPolicy,
    maximum_depth: int,
    expected_root: str | None = None,
    selection_root_id: str | None = None,
) -> tuple[str, str, tuple[Wave4VariantDescriptor, ...]]:
    """Validate the complete cheap descriptor terminal used for preselection."""

    if operation_id not in WAVE4_OPERATIONS:
        raise OrbitError(f"{operation_id!r} is not a Wave 4 orbit operation")
    kind = payload.get("kind")
    status = payload.get("status")
    if (kind, status) not in {
        ("wave4_descriptor_root", "described"),
        ("wave4_root", "retained"),
    }:
        raise OrbitError("Wave 4 validator requires a described or retained root terminal")
    if payload.get("operation_id") != operation_id:
        raise OrbitError("Wave 4 root operation does not match the requested operation")
    negative_operation = SQUARE_OPERATIONS[operation_id]["negative"]
    if negative_operation == "N19_WHOLE_CLAIM_NEGATION_V1":
        raise OrbitError("N19 is forbidden in Wave 4")
    if payload.get("negative_operation") != negative_operation:
        raise OrbitError("Wave 4 root changes the configured negative operation")
    root = _wave4_text(payload.get("root"), "wave4.root")
    if expected_root is not None and root != expected_root:
        raise OrbitError("Wave 4 root terminal names a different requested root")
    if payload.get("certificate_phase") not in {None, "selected_only"}:
        raise OrbitError("Wave 4 root claims an unknown certificate phase")
    if kind == "wave4_descriptor_root" or "descriptors" in payload:
        variants_raw = _wave4_sequence(payload.get("descriptors"), "wave4.descriptors")
        count = _wave4_nat(
            payload.get("enumerated_descriptor_count"),
            "wave4.enumerated_descriptor_count",
        )
    else:
        # Backward-compatible validation for pre-split development fixtures.
        variants_raw = _wave4_sequence(payload.get("variants"), "wave4.variants")
        count = _wave4_nat(
            payload.get("enumerated_variant_count"), "wave4.enumerated_variant_count"
        )
    if count != len(variants_raw) or count == 0:
        raise OrbitError("Wave 4 root enumeration count is incomplete")
    descriptors = tuple(
        _describe_wave4_variant(
            value,
            root=root,
            operation_id=operation_id,
            negative_operation=negative_operation,
            policy=policy,
            maximum_depth=maximum_depth,
            selection_root_id=selection_root_id or root,
        )
        for value in variants_raw
    )
    if [descriptor.index for descriptor in descriptors] != list(range(len(descriptors))):
        raise OrbitError("Wave 4 variant indices are not a complete canonical enumeration")

    by_selection: dict[str, Wave4VariantDescriptor] = {}
    for descriptor in descriptors:
        previous = by_selection.get(descriptor.selection_hash)
        if previous is not None:
            raise OrbitError("Wave 4 enumerates one exact operation/site identity twice")
        by_selection[descriptor.selection_hash] = descriptor
    if len({descriptor.base_edge_hash for descriptor in descriptors}) != 1:
        raise OrbitError("Wave 4 variants disagree on their shared certified negative edge")
    return root, negative_operation, descriptors


def preselect_wave4_variant_descriptors(
    payload: Mapping[str, Any],
    *,
    operation_id: str,
    policy: OrbitPolicy,
    maximum_depth: int,
    expected_root: str | None = None,
    selection_root_id: str | None = None,
) -> tuple[Wave4VariantDescriptor, ...]:
    """Select at most five descriptors before full proof parsing and frozen rendering."""

    root, _negative_operation, descriptors = _describe_wave4_root_payload(
        payload,
        operation_id=operation_id,
        policy=policy,
        maximum_depth=maximum_depth,
        expected_root=expected_root,
        selection_root_id=selection_root_id,
    )
    unique: dict[str, Wave4VariantDescriptor] = {}
    for descriptor in descriptors:
        unique.setdefault(descriptor.selection_hash, descriptor)
    ranked = sorted(
        unique.values(),
        key=lambda descriptor: (
            hash_canonical(
                {
                    "kind": "sft1_wave4_stable_selection_rank_v1",
                    "salt": policy.selection_salt,
                    "root_id": selection_root_id or root,
                    "selection_hash": descriptor.selection_hash,
                }
            ),
            descriptor.selection_hash,
            descriptor.content_hash,
        ),
    )
    return tuple(ranked[: policy.maximum_variants_per_root])


def select_wave4_variants(
    validated: ValidatedWave4Root, policy: OrbitPolicy
) -> tuple[ValidatedWave4Variant, ...]:
    """Collapse exact repeats and select the stable max-five complete closures."""

    unique: dict[str, ValidatedWave4Variant] = {}
    for variant in validated.variants:
        previous = unique.get(variant.selection_hash)
        if previous is None:
            unique[variant.selection_hash] = variant
        elif previous.content_hash != variant.content_hash:
            raise OrbitError("one exact Wave 4 operation/site identity has conflicting evidence")
    ranked = sorted(
        unique.values(),
        key=lambda variant: (
            hash_canonical(
                {
                    "kind": "sft1_wave4_stable_selection_rank_v1",
                    "salt": policy.selection_salt,
                    "root_id": validated.selection_root_id,
                    "selection_hash": variant.selection_hash,
                }
            ),
            variant.selection_hash,
            variant.content_hash,
        ),
    )
    return tuple(ranked[: policy.maximum_variants_per_root])


# ------------------------------------------------------------------ eligibility


def eligible_roots(
    loaded: LoadedConfig[SprintConfig],
    run_ids: Sequence[str] = SOURCE_RUNS,
    negative_operation: str = "N25_TOGGLE_EQ_NE_PROOF_V1",
    source_staging_root: Path | None = None,
) -> list[dict[str, Any]]:
    """Certified roots of one negative operation from the source runs, deduplicated by
    the reference closed-Expr hash and ordered by a stable hash of that identity."""

    allowed_directions = {"eq_to_ne", "ne_to_eq"} if negative_operation.startswith("N25") else None
    staging = source_staging_root or Path(loaded.config.output.staging_root)
    best: dict[str, dict[str, Any]] = {}
    for run_id in run_ids:
        paths = RunPaths(staging, run_id)
        if not paths.run_manifest.is_file() or not paths.journal.is_file():
            raise SquareError(f"certified source run {run_id!r} lacks its manifest or journal")
        terminal_pairs: set[str] = set()
        for terminal in paths.journal.read_text(encoding="utf-8").splitlines():
            if not terminal.strip():
                continue
            event = json.loads(terminal)
            if (
                event.get("kind") == "terminal"
                and event.get("status") == "retained"
                and event.get("operation_id") == negative_operation
            ):
                terminal_pairs.add(str(event.get("pair_id")))
        manifest_sha256 = hash_file(paths.run_manifest)
        for record in read_retained(paths.retained):
            if record["operation_id"] != negative_operation:
                continue
            sidecar = record["sidecar"]
            if str(sidecar.get("pair_id")) not in terminal_pairs:
                continue
            direction = str((sidecar.get("site") or {}).get("detail", ""))
            if allowed_directions is not None and direction not in allowed_directions:
                continue
            key = str(sidecar["repr"]["reference"]["provenance"]["expr_hash"])
            entry = {
                "name": str(sidecar["root_name"]),
                "direction": direction,
                "reference_expr_hash": key,
                "source_run": run_id,
                "source_run_manifest_sha256": manifest_sha256,
                "source_pair_id": str(record["row"]["pair_id"]),
                "row_hash": str(record["row_hash"]),
                "statement": sidecar.get("statement"),
            }
            if key not in best or entry["row_hash"] < best[key]["row_hash"]:
                best[key] = entry
    roots = sorted(
        best.values(), key=lambda item: hash_canonical([SQUARE_SALT, item["reference_expr_hash"]])
    )
    return roots


def inventory_roots(repo_root: Path, loaded: LoadedConfig[SprintConfig]) -> list[dict[str, Any]]:
    """Every inventory theorem in the sprint's deterministic pool order (N19 census)."""
    base = SprintRunner(repo_root, loaded, run_id="square-n19-census")
    return [
        {
            "name": name,
            "direction": "whole_claim",
            "reference_expr_hash": name,
            "source_run": "inventory",
            "pool": pool,
        }
        for name, pool in base.root_order()
    ]


def write_census(
    loaded: LoadedConfig[SprintConfig],
    out: Path,
    operation_id: str = SQUARE_OPERATION,
    repo_root: Path | None = None,
    source_run_ids: Sequence[str] = SOURCE_RUNS,
    source_staging_root: Path | None = None,
) -> dict[str, Any]:
    negative = SQUARE_OPERATIONS[operation_id]["negative"]
    if negative in INVENTORY_NEGATIVES:
        if repo_root is None:
            raise SquareError("inventory census needs the repository root")
        roots = inventory_roots(repo_root, loaded)
    else:
        roots = eligible_roots(
            loaded,
            run_ids=source_run_ids,
            negative_operation=negative,
            source_staging_root=source_staging_root,
        )
    payload = {
        "schema_version": 1,
        "operation_id": operation_id,
        "negative_operation": negative,
        "source_runs": list(source_run_ids),
        "source_staging_root": str(source_staging_root or Path(loaded.config.output.staging_root)),
        "count": len(roots),
        "by_direction": _count_by(roots, "direction"),
        "roots_sha256": hash_canonical([item["name"] for item in roots]),
        "roots": roots,
    }
    write_atomic(out, canonical_json_bytes(payload) + b"\n")
    return {k: v for k, v in payload.items() if k != "roots"}


# ------------------------------------------------------------------ runner


def wave4_process_body(names: Sequence[str], operation_id: str, maximum_depth: int) -> str:
    """One batched proof-free descriptor request; selection happens afterwards."""

    if operation_id not in WAVE4_OPERATIONS:
        raise OrbitError(f"{operation_id!r} is not a Wave 4 orbit operation")
    if not 1 <= maximum_depth <= 3:
        raise OrbitError("Wave 4 process depth must be between one and three")
    literals = ", ".join(lean_string_literal(name) for name in names)
    return (
        "run_meta do\n  LeanFaith.SFT1.Sprint.processWave4DescriptorRoots "
        f"#[{literals}] {json.dumps(operation_id)} {maximum_depth}"
    )


def wave4_render_body(
    name: str,
    selected_indices: Sequence[int],
    scope: str,
    operation_id: str,
    maximum_depth: int,
) -> str:
    """Rebuild and freeze-render exactly the selected enumeration indices."""

    if operation_id not in WAVE4_OPERATIONS:
        raise OrbitError(f"{operation_id!r} is not a Wave 4 orbit operation")
    if not selected_indices or len(selected_indices) > 5:
        raise OrbitError("Wave 4 render needs between one and five selected variants")
    if len(set(selected_indices)) != len(selected_indices) or any(
        type(index) is not int or index < 0 for index in selected_indices
    ):
        raise OrbitError("Wave 4 render indices must be unique nonnegative integers")
    indices = ", ".join(str(index) for index in selected_indices)
    lines = [
        "run_meta do",
        f"  let indices : Array Nat := #[{indices}]",
        "  let orbits ← LeanFaith.SFT1.Sprint.rebuildSelectedWave4Orbits "
        f"{lean_string_literal(name)} {json.dumps(operation_id)} {maximum_depth} indices",
        "  LeanFaith.SFT1.Sprint.emitSelectedWave4Report "
        f"{lean_string_literal(name)} {json.dumps(operation_id)} {maximum_depth} indices orbits",
    ]
    fields = {"p": "p", "c": "c", "p_prime": "pPrime", "c_prime": "cPrime"}
    for slot, _orbit_index in enumerate(selected_indices):
        lines.append(
            f"  let some orbit{slot} := orbits[{slot}]? | "
            f'throwError "missing selected Wave 4 orbit at slot {slot}"'
        )
        for endpoint, lean_field in fields.items():
            lines.append(
                f"  LeanFaith.GoalV1.emitClosedProp {json.dumps(f'{slot}.{endpoint}')} "
                f"{json.dumps(scope)} {json.dumps(ENDPOINT_ORIGIN[endpoint])} "
                f"orbit{slot}.{lean_field}"
            )
    return "\n".join(lines)


def wave4_render_inputs(
    name: str, selected_count: int, statements: Mapping[str, str]
) -> tuple[ClosedExprInput, ...]:
    """Frozen-render inputs for the selected variants of one ancestry root."""

    if not 1 <= selected_count <= 5:
        raise OrbitError("Wave 4 render input count must be between one and five")
    statement = statements.get(name) or f"theorem {name} : <statement text unavailable>"
    inputs: list[ClosedExprInput] = []
    for slot in range(selected_count):
        for endpoint in ("p", "c", "p_prime", "c_prime"):
            if endpoint == "p":
                material = ClosedExprSourceMaterial(kind="raw_statement", raw_statement=statement)
            else:
                material = ClosedExprSourceMaterial(
                    kind="constructed_expr_no_source_text",
                    absence_reason=(
                        f"Wave 4 endpoint {endpoint} constructed from {name} by a checked orbit"
                    ),
                )
            inputs.append(
                ClosedExprInput(
                    endpoint_id=f"{slot}.{endpoint}",
                    endpoint_role=cast(Any, ENDPOINT_ROLE[endpoint]),
                    expr_origin=cast(Any, ENDPOINT_ORIGIN[endpoint]),
                    source_material=material,
                )
            )
    return tuple(inputs)


def _wave4_selected_report(raw_response_path: str | None, *, root: str) -> dict[str, Any]:
    """Read the one full selected-certificate report from the durable response."""

    if not raw_response_path:
        raise GoalV1Error("Wave 4 selected request has no durable raw response")
    path = Path(raw_response_path)
    if not path.is_file():
        raise GoalV1Error(f"Wave 4 selected raw response is absent: {path}")
    try:
        raw = read_json_object(path)
        response = _wave4_mapping(raw.get("response"), "wave4.raw.response")
        messages = cast(Sequence[dict[str, Any]], response.get("messages") or ())
        reports = [
            dict(payload)
            for payload in parse_evidence_lines(messages)
            if payload.get("kind") == "wave4_selected_root" and payload.get("root") == root
        ]
    except (OSError, ValueError, TypeError) as exc:
        raise GoalV1Error(f"Wave 4 selected raw response is malformed: {exc}") from exc
    if len(reports) != 1:
        raise GoalV1Error(
            f"Wave 4 selected request emitted {len(reports)} matching certificate reports"
        )
    return reports[0]


def combine_wave4_selected_payload(
    descriptor_payload: Mapping[str, Any],
    selected_payload: Mapping[str, Any],
    *,
    expected_indices: Sequence[int],
) -> dict[str, Any]:
    """Bind a selected proof report to its complete proof-free enumeration."""

    if (
        descriptor_payload.get("kind") != "wave4_descriptor_root"
        or descriptor_payload.get("status") != "described"
    ):
        raise OrbitError("Wave 4 combination requires a described first-phase payload")
    if (
        selected_payload.get("kind") != "wave4_selected_root"
        or selected_payload.get("status") != "retained"
    ):
        raise OrbitError("Wave 4 combination requires a retained selected report")
    exact_indices = [
        _wave4_nat(value, "wave4.selected_descriptor_indices")
        for value in _wave4_sequence(
            selected_payload.get("selected_descriptor_indices"),
            "wave4.selected_descriptor_indices",
        )
    ]
    if exact_indices != list(expected_indices):
        raise OrbitError("Wave 4 selected report differs from the requested descriptor order")
    variants = list(_wave4_sequence(selected_payload.get("variants"), "wave4.variants"))
    if _wave4_nat(
        selected_payload.get("selected_variant_count"), "wave4.selected_variant_count"
    ) != len(variants) or len(variants) != len(exact_indices):
        raise OrbitError("Wave 4 selected report count is incomplete")
    for identity_field in (
        "schema_version",
        "operation_id",
        "negative_operation",
        "engine_semantic_version",
        "root",
        "module",
        "level_params",
        "certificate_phase",
    ):
        if descriptor_payload.get(identity_field) != selected_payload.get(identity_field):
            raise OrbitError(f"Wave 4 selected report changes first-phase {identity_field}")
    descriptors = list(_wave4_sequence(descriptor_payload.get("descriptors"), "wave4.descriptors"))
    count = _wave4_nat(
        descriptor_payload.get("enumerated_descriptor_count"),
        "wave4.enumerated_descriptor_count",
    )
    if count != len(descriptors) or not descriptors:
        raise OrbitError("Wave 4 descriptor enumeration is incomplete")
    return {
        "schema_version": 1,
        "kind": "wave4_root",
        "status": "retained",
        "reason": "",
        "operation_id": descriptor_payload["operation_id"],
        "negative_operation": descriptor_payload["negative_operation"],
        "engine_semantic_version": descriptor_payload["engine_semantic_version"],
        "root": descriptor_payload["root"],
        "module": descriptor_payload["module"],
        "level_params": descriptor_payload["level_params"],
        "descriptors": descriptors,
        "enumerated_descriptor_count": count,
        "selected_descriptor_indices": exact_indices,
        "selected_variant_count": len(variants),
        "variants": variants,
        "certificate_phase": "selected_only",
        "descriptor_elapsed_ms": descriptor_payload.get("elapsed_ms"),
    }


def process_body(names: Sequence[str], operation_id: str = SQUARE_OPERATION) -> str:
    literals = ", ".join(lean_string_literal(name) for name in names)
    return (
        "run_meta do\n  LeanFaith.SFT1.Sprint.processSquares "
        f"#[{literals}] {json.dumps(operation_id)}"
    )


def render_body(names: Sequence[str], scope: str, operation_id: str = SQUARE_OPERATION) -> str:
    literals = ", ".join(lean_string_literal(name) for name in names)
    lines = [
        "run_meta do",
        f"  let squares ← LeanFaith.SFT1.Sprint.rebuildSquares #[{literals}]"
        f" {json.dumps(operation_id)}",
        "  LeanFaith.SFT1.Sprint.emitSquareReport squares",
    ]
    fields = {"p": "p", "c": "c", "p_prime": "pPrime", "c_prime": "cPrime"}
    for index in range(len(names)):
        for endpoint, lean_field in fields.items():
            lines.append(
                f"  LeanFaith.GoalV1.emitClosedProp {json.dumps(f'{index}.{endpoint}')} "
                f"{json.dumps(scope)} {json.dumps(ENDPOINT_ORIGIN[endpoint])} "
                f"(squares[{index}]!).{lean_field}"
            )
    return "\n".join(lines)


def render_inputs(
    names: Sequence[str], statements: Mapping[str, str]
) -> tuple[ClosedExprInput, ...]:
    inputs: list[ClosedExprInput] = []
    for index, name in enumerate(names):
        statement = statements.get(name) or f"theorem {name} : <statement text unavailable>"
        for endpoint in ("p", "c", "p_prime", "c_prime"):
            if endpoint == "p":
                material = ClosedExprSourceMaterial(kind="raw_statement", raw_statement=statement)
            else:
                material = ClosedExprSourceMaterial(
                    kind="constructed_expr_no_source_text",
                    absence_reason=(
                        f"square endpoint {endpoint} constructed by the sprint engine from {name}"
                    ),
                )
            inputs.append(
                ClosedExprInput(
                    endpoint_id=f"{index}.{endpoint}",
                    endpoint_role=cast(Any, ENDPOINT_ROLE[endpoint]),
                    expr_origin=cast(Any, ENDPOINT_ORIGIN[endpoint]),
                    source_material=material,
                )
            )
    return tuple(inputs)


class SquareRunner:
    """Journaled, cached, resumable square construction over one persistent worker."""

    def __init__(
        self,
        repo_root: Path,
        loaded: LoadedConfig[SprintConfig],
        *,
        run_id: str,
        roots: Sequence[Mapping[str, Any]],
        max_roots: int | None = None,
        owner_session: str = "claude-sft1-square",
        use_cache: bool = True,
        operation_id: str = SQUARE_OPERATION,
        cache_schema: int = SQUARE_CACHE_SCHEMA,
        isolated_cache: bool = False,
    ) -> None:
        if operation_id not in SQUARE_OPERATIONS:
            raise SquareError(f"unknown square operation {operation_id!r}")
        self.base = SprintRunner(repo_root, loaded, run_id=run_id, owner_session=owner_session)
        self.use_cache = use_cache
        self.operation_id = operation_id
        self.cache_schema = cache_schema
        self.isolated_cache = isolated_cache
        self.repo_root = repo_root
        self.loaded = loaded
        self.config = loaded.config
        self.run_id = run_id
        self.roots = list(roots)
        self.max_roots = max_roots
        self.paths = self.base.paths
        self.journal = self.base.journal
        # fixture gates write to their own cache root so they can never overwrite a record
        # that a release references
        self.cache = (
            SemanticCache(Path(loaded.config.output.staging_root) / "cache_fixtures")
            if isolated_cache
            else self.base.cache
        )
        self.done: dict[str, str] = {}
        self.retained = 0
        self.counts: dict[str, int] = {}
        self.lean_roots = 0
        self.cache_roots = 0
        self.started = time.monotonic()
        self.batches = 0
        self.statements: dict[str, str] = {}
        self.recovered_roots: list[str] = []

    # ---------------------------------------------------------------- state

    def load_state(self) -> None:
        begun: dict[str, dict[str, Any]] = {}
        for record in self.journal.read():
            kind = record.get("kind")
            if kind == "square_begin":
                begun[str(record["root"])] = record
                continue
            if kind != "square_terminal":
                continue
            name = str(record["root"])
            begun.pop(name, None)
            if name in self.done:
                continue
            self.done[name] = str(record["status"])
            self.counts[self.done[name]] = self.counts.get(self.done[name], 0) + 1
            if self.done[name] == "retained":
                self.retained += 1
            if record.get("source") == "cache":
                self.cache_roots += 1
            else:
                self.lean_roots += 1
        if begun:
            self.recover_in_flight(begun)
        inventory_dir = Path(self.config.inventory.root) / self.config.project.project_revision
        for row in load_inventory(inventory_dir / "inventory.jsonl"):
            self.statements.setdefault(str(row["name"]), str(row["statement"]))

    def recover_in_flight(self, begun: Mapping[str, Mapping[str, Any]]) -> None:
        """Close per-root transactions interrupted between the row append and the terminal.

        A root whose ``square_begin`` promised rows that are all present in the retained
        file gets its terminal written now; otherwise its orphaned rows are ignored by
        every reader (terminals are the authority) and the root is processed again.
        """
        present: dict[str, set[str]] = {}
        wanted = {str(root) for root in begun}
        for item in read_retained(self.paths.retained):
            sidecar = cast(dict[str, Any], item["sidecar"])
            root = str(sidecar.get("root_name"))
            if root in wanted:
                present.setdefault(root, set()).add(str(sidecar.get("pair_id")))
        for root, record in begun.items():
            expected = {str(pair) for pair in record.get("pair_ids", [])}
            if expected and expected <= present.get(root, set()):
                self.journal.append(
                    {
                        "kind": "square_terminal",
                        "root": root,
                        "status": "retained",
                        "reason": "",
                        "source": "recovered",
                        "batch": int(record.get("batch", 0)),
                        "pair_ids": sorted(expected),
                    }
                )
                self.done[root] = "retained"
                self.counts["retained"] = self.counts.get("retained", 0) + 1
                self.retained += 1
                self.lean_roots += 1
            else:
                self.journal.append(
                    {"kind": "square_abandoned", "root": root, "pair_ids": sorted(expected)}
                )

    def square_root_key(self, name: str) -> str:
        return square_cache_key(
            operation_id=self.operation_id,
            name=name,
            engine_semantic_version=self.base.identity.semantic_version,
            project_revision=self.base.pins.project_revision,
            lean_version=self.base.pins.lean_version,
            import_options_fingerprint=self.base.identity.import_options_fingerprint,
            revision=operation_cache_revision(self.operation_id),
            schema=self.cache_schema,
            engine_source_sha256=self.base.identity.source_sha256,
            compile_context_id=self.base.context.compile_context_id,
        )

    def cache_put(self, name: str, record: Mapping[str, Any]) -> str:
        """Write-once semantics: an existing record is never overwritten.

        Keys of schema 3 name one deterministic computation, so a colliding write can only
        differ in volatile fields; keeping the first record protects every release that
        already references it. The skipped write is journaled.
        """
        key = self.square_root_key(name)
        existing = self.cache.get_root(key)
        if existing is None:
            self.cache.put_root(key, record)
            return "written"
        if existing.get("process_request_hash") == record.get("process_request_hash") and (
            (existing.get("render") or {}).get("request_hash")
            == (record.get("render") or {}).get("request_hash")
        ):
            return "identical"
        self.journal.append(
            {
                "kind": "cache_write_skipped",
                "root": name,
                "cache_key": key,
                "existing_process_request_hash": existing.get("process_request_hash"),
                "new_process_request_hash": record.get("process_request_hash"),
            }
        )
        return "kept_existing"

    # ---------------------------------------------------------------- run

    def run(self, *, require_zero_lean: bool = False) -> dict[str, Any]:
        self.base.verify_pins()
        self.load_state()
        self.write_run_manifest(replay=require_zero_lean)
        reservation: Reservation | None = None
        pending: list[Mapping[str, Any]] = []
        considered = 0
        try:
            for root in self.roots:
                if self.max_roots is not None and considered >= self.max_roots:
                    break
                considered += 1
                name = str(root["name"])
                if name in self.done:
                    continue
                if self.try_cache(root):
                    continue
                if require_zero_lean:
                    raise SquareError(f"replay would need Lean for root {name!r}")
                pending.append(root)
                if len(pending) >= self.config.execution.batch_roots:
                    if reservation is None:
                        reservation = self.base.claim()
                    self.process_batch(pending)
                    pending = []
            if pending:
                if reservation is None:
                    reservation = self.base.claim()
                self.process_batch(pending)
            return self.write_status(final=True, replay=require_zero_lean)
        finally:
            self.base.close_session()
            if reservation is not None:
                self.base.release(reservation)

    def manifest_identity(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "cache_schema": self.cache_schema,
            "config_semantic_hash": self.loaded.config_hash,
            "engine_source_sha256": self.base.identity.source_sha256,
            "engine_semantic_version": self.base.identity.semantic_version,
            "import_options_fingerprint": self.base.identity.import_options_fingerprint,
            "roots_sha256": hash_canonical([str(item["name"]) for item in self.roots]),
            "root_count": len(self.roots),
        }

    def write_run_manifest(self, *, replay: bool = False) -> None:
        if self.paths.run_manifest.is_file():
            recorded = read_json_object(self.paths.run_manifest)
            mismatches = []
            engine_keys = {
                "engine_source_sha256",
                "engine_semantic_version",
                "import_options_fingerprint",
            }
            for key, value in self.manifest_identity().items():
                if replay and key in engine_keys:
                    continue  # a zero-Lean replay never elaborates; the engine text is moot
                stored = (
                    recorded.get(key)
                    if key
                    not in {
                        "engine_source_sha256",
                        "engine_semantic_version",
                        "import_options_fingerprint",
                    }
                    else (recorded.get("engine") or {}).get(key.removeprefix("engine_"))
                )
                if stored != value:
                    mismatches.append(f"{key}: manifest {stored!r} != current {value!r}")
            if mismatches:
                raise SquareError(
                    f"run {self.run_id!r} cannot resume: run manifest mismatch; "
                    + "; ".join(mismatches)
                    + ". Use a new run id for a different config, engine, or root list."
                )
            return
        manifest = {
            "schema_version": 1,
            "sprint_id": self.config.sprint_id,
            "run_id": self.run_id,
            "operation_id": self.operation_id,
            "started_at": utc_now(),
            "config_semantic_hash": self.loaded.config_hash,
            "engine": self.base.identity.to_dict(),
            "project": self.base.pins.to_dict(),
            "implementation_commit": _git(self.repo_root, "rev-parse", "HEAD"),
            "implementation_dirty": bool(_git(self.repo_root, "status", "--porcelain")),
            "max_roots": self.max_roots,
            "roots_sha256": hash_canonical([str(item["name"]) for item in self.roots]),
            "root_count": len(self.roots),
            "cache_schema": self.cache_schema,
            "cache_root": str(self.cache.root),
            "argv": sys.argv,
        }
        write_atomic(self.paths.run_manifest, canonical_json_bytes(manifest) + b"\n")

    # ---------------------------------------------------------------- cache

    def try_cache(self, root: Mapping[str, Any]) -> bool:
        if not self.use_cache:
            return False  # fixture gates must exercise the live engine
        name = str(root["name"])
        record = self.cache.get_root(self.square_root_key(name))
        if record is None:
            return False
        if not cacheable_status(record.get("status")):
            return False
        if record.get("status") == "retained" and not isinstance(record.get("render"), dict):
            return False
        self.finalize(name, record, source="cache", root=root)
        return True

    # ---------------------------------------------------------------- batches

    def process_batch(self, batch: Sequence[Mapping[str, Any]]) -> None:
        session = self.base.open_session()
        self.batches += 1
        names = [str(item["name"]) for item in batch]
        request = LeanRequest(
            request_id=f"{self.run_id}:square:{self.batches}:" + hash_canonical(names)[:16],
            context_id=self.base.context.compile_context_id,
            code=engine_module.command_text(
                self.base.context, process_body(names, self.operation_id)
            ),
            allow_sorry=False,
            timeout_seconds=self.config.execution.request_timeout_seconds,
            metadata={"sprint_phase": "square_process"},
        )
        result = session.backend.run(request)
        session.request_count += 1
        session.lean_elapsed_ms += result.elapsed_ms
        payloads: dict[str, dict[str, Any]] = {}
        if result.status in {LeanStatus.VALID, LeanStatus.INVALID}:
            for line_payload in parse_evidence_lines(result.messages):
                if line_payload.get("kind") == "square":
                    payloads[str(line_payload["root"])] = line_payload
        missing = [name for name in names if name not in payloads]
        retryable_infrastructure = result.status in {
            LeanStatus.CRASH,
            LeanStatus.INTERNAL_ERROR,
            LeanStatus.TIMEOUT,
        }
        valid_partial_payload = result.status == LeanStatus.VALID and bool(missing)
        if (retryable_infrastructure or valid_partial_payload) and len(batch) > 1:
            half = len(batch) // 2
            self.batches -= 1
            self.process_batch(batch[:half])
            self.process_batch(batch[half:])
            return
        pending: list[tuple[Mapping[str, Any], dict[str, Any]]] = []
        for root in batch:
            name = str(root["name"])
            payload: dict[str, Any] | None = payloads.get(name)
            if payload is None:
                errors = [
                    str(m.get("data", ""))[:300]
                    for m in result.messages
                    if str(m.get("severity", "")) == "error"
                ]
                payload = {
                    "status": "error",
                    "reason": f"request_{result.status.value}:{'; '.join(errors[:2])}",
                }
            if payload.get("status") != "retained":
                record = self.cache_record(name, payload, None, result.request_hash)
                if cacheable_status(payload.get("status")):
                    self.cache_put(name, record)
                self.finalize(name, record, source="lean", root=root)
                continue
            violation = self.screen_payload(payload)
            if violation is not None:
                payload = dict(payload)
                payload["status"] = "rejected"
                payload["reason"] = violation
                record = self.cache_record(name, payload, None, result.request_hash)
                self.cache_put(name, record)
                self.finalize(name, record, source="lean", root=root)
                continue
            pending.append((root, payload))
        size = max(1, self.config.execution.render_batch_pairs // 2)
        for start in range(0, len(pending), size):
            chunk = pending[start : start + size]
            if len(chunk) == 1 or not self.render_chunk(chunk, result.request_hash):
                for item in chunk:
                    self.render_chunk([item], result.request_hash, final=True)
        self.write_status(final=False)

    def screen_payload(self, payload: Mapping[str, Any]) -> str | None:
        goals = cast(dict[str, str], payload["goals"])
        canonical: dict[str, str] = {}
        for endpoint, text in goals.items():
            surface, violation = canonical_surface(text)
            if surface is None:
                return f"screen_{endpoint}:{violation}"
            residue = residue_violation(surface)
            if residue is not None:
                return f"screen_{endpoint}:{residue}"
            if self.base.gold.hit(surface):
                return f"screen_{endpoint}:gold_blocklist"
            canonical[endpoint] = surface
        if len(set(canonical.values())) != 4:
            return "screen:square_endpoints_not_pairwise_distinct"
        return None

    def render_chunk(
        self,
        chunk: Sequence[tuple[Mapping[str, Any], dict[str, Any]]],
        process_request_hash: str,
        *,
        final: bool = False,
    ) -> bool:
        session = self.base.open_session()
        names = [str(root["name"]) for root, _ in chunk]
        request_id = f"{self.run_id}:square_render:{self.batches}:" + hash_canonical(names)[:16]
        scope = self.base.scope + ":square"
        try:
            batch = render_closed_expr_in_session(
                session.backend,
                inputs=render_inputs(names, self.statements),
                compile_context=self.base.context,
                render_scope_id=scope,
                session_body=render_body(names, scope, self.operation_id),
                request_id=request_id,
                timeout_seconds=self.config.execution.request_timeout_seconds,
            )
        except (GoalV1Error, ValueError) as exc:
            if not final:
                return False
            root, payload = chunk[0]
            self.reject(root, payload, f"render_failed:route:{str(exc)[:300]}")
            return True
        session.request_count += 1
        session.lean_elapsed_ms += batch.elapsed_ms
        if batch.failures:
            if not final:
                return False
            root, payload = chunk[0]
            detail = "; ".join(f"{f.endpoint_id}: {f.detail}" for f in batch.failures)[:400]
            self.reject(root, payload, f"render_failed:{detail}")
            return True
        sidecars = {sidecar.record.endpoint_id: sidecar for sidecar in batch.sidecars}
        for index, (root, payload) in enumerate(chunk):
            name = str(root["name"])
            endpoints = {
                ep: sidecars.get(f"{index}.{ep}") for ep in ("p", "c", "p_prime", "c_prime")
            }
            if any(value is None for value in endpoints.values()):
                self.reject(root, payload, "render_missing_endpoint")
                continue
            texts_ok = all(
                cast(ClosedExprSidecar, endpoints[ep]).core_text()
                == canonical_surface(str(payload["goals"][ep]))[0]
                for ep in endpoints
            )
            if not texts_ok:
                self.reject(root, payload, "render_text_mismatch")
                continue
            render = {
                ep: {
                    "record": cast(ClosedExprSidecar, sc).record.to_dict(),
                    "source_material": cast(ClosedExprSidecar, sc).source_material.to_dict(),
                }
                for ep, sc in endpoints.items()
            }
            render["request_hash"] = batch.request_hash  # type: ignore[assignment]
            record = self.cache_record(name, payload, render, process_request_hash)
            self.cache_put(name, record)
            self.finalize(name, record, source="lean", root=root)
        return True

    def reject(self, root: Mapping[str, Any], payload: dict[str, Any], reason: str) -> None:
        name = str(root["name"])
        self.journal.append(
            {
                "kind": "square_terminal",
                "root": name,
                "status": "rejected",
                "reason": reason,
                "source": "lean",
                "batch": self.batches,
            }
        )
        self.done[name] = "rejected"
        self.counts["rejected"] = self.counts.get("rejected", 0) + 1
        self.lean_roots += 1

    def cache_record(
        self,
        name: str,
        payload: Mapping[str, Any],
        render: Mapping[str, Any] | None,
        process_request_hash: str,
    ) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "operation_id": self.operation_id,
            "root": name,
            "status": payload.get("status"),
            "reason": payload.get("reason", ""),
            "direction": payload.get("direction"),
            "module": payload.get("module"),
            "level_params": payload.get("level_params"),
            "alpha": payload.get("alpha"),
            "goals": payload.get("goals"),
            "evidence": payload.get("evidence"),
            "elapsed_ms": payload.get("elapsed_ms"),
            "engine": self.base.identity.to_dict(),
            "implementation_commit": self.base.implementation_commit,
            "process_request_hash": process_request_hash,
            "render": dict(render) if render is not None else None,
        }

    # ---------------------------------------------------------------- rows

    def finalize(
        self, name: str, record: Mapping[str, Any], *, source: str, root: Mapping[str, Any]
    ) -> None:
        status = str(record.get("status"))
        if status != "retained":
            self.journal.append(
                {
                    "kind": "square_terminal",
                    "root": name,
                    "status": status,
                    "reason": record.get("reason", ""),
                    "source": source,
                    "batch": self.batches,
                }
            )
            self.done[name] = status
            self.counts[status] = self.counts.get(status, 0) + 1
            if source == "cache":
                self.cache_roots += 1
            else:
                self.lean_roots += 1
            return
        rows = self.build_rows(name, record, root)
        self.journal.append(
            {
                "kind": "square_begin",
                "root": name,
                "batch": self.batches,
                "pair_ids": [item["sidecar"]["pair_id"] for item in rows],
                "row_hashes": [item["row_hash"] for item in rows],
            }
        )
        with self.paths.retained.open("ab") as handle:
            for row_record in rows:
                handle.write(canonical_json_bytes(row_record) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        self.journal.append(
            {
                "kind": "square_terminal",
                "root": name,
                "status": "retained",
                "reason": "",
                "source": source,
                "batch": self.batches,
                "pair_ids": [item["sidecar"]["pair_id"] for item in rows],
            }
        )
        self.done[name] = "retained"
        self.counts["retained"] = self.counts.get("retained", 0) + 1
        self.retained += 1
        if source == "cache":
            self.cache_roots += 1
        else:
            self.lean_roots += 1

    def build_rows(
        self,
        name: str,
        record: Mapping[str, Any],
        root: Mapping[str, Any],
        reconciliation: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        render = cast(dict[str, Any], record["render"])
        evidence = cast(dict[str, Any], record["evidence"])
        direction = str(record["direction"])
        operation_id = str(record.get("operation_id") or self.operation_id)
        if operation_id != self.operation_id:
            raise SquareError(f"cache record operation {operation_id} != {self.operation_id}")
        family_prefix = SQUARE_OPERATIONS[operation_id]["family"]
        if operation_id == SQUARE_OPERATION:
            core_family = f"square_{'eq' if direction == 'eq_to_ne' else 'ne'}"
        else:
            core_family = (
                f"{family_prefix}_{TRANSFORM_SHORT.get(str(evidence.get('t_p')), 'other')}"
            )
        root_id = self.base.root_id(name)
        cache_key = self.square_root_key(name)
        cache_block = {
            "kind": SQUARE_CACHE_KIND,
            "schema": self.cache_schema,
            "revision": operation_cache_revision(operation_id),
            "key": cache_key,
            "path": f"roots/{cache_key[:2]}/{cache_key}.json",
            # content address of the record these rows were built from; a release snapshots
            # the record under this hash so later cache writes cannot invalidate it
            "content_sha256": hash_canonical(record),
            "snapshot": None,
        }
        rows: list[dict[str, Any]] = []
        for kind, label, ref_ep, cand_ep, evidence_key in ROW_KINDS:
            reference = cast(dict[str, Any], render[ref_ep])["record"]
            candidate = cast(dict[str, Any], render[cand_ep])["record"]
            ref_text = str(reference["goal_v1"])
            cand_text = str(candidate["goal_v1"])
            ref_hash = str(reference["provenance"]["expr_hash"])
            cand_hash = str(candidate["provenance"]["expr_hash"])
            if ref_hash == cand_hash or ref_text == cand_text:
                raise SquareError(f"self pair inside square {name}/{kind}")
            pair_id = make_id(
                PAIR_PREFIX,
                {
                    "root_id": root_id,
                    "operation_id": operation_id,
                    "row_kind": kind,
                    "reference_expr_hash": ref_hash,
                    "candidate_expr_hash": cand_hash,
                },
            )
            check = cast(dict[str, Any], evidence[evidence_key])
            reference_truth = ENDPOINT_TRUTH[ref_ep]
            candidate_truth = ENDPOINT_TRUTH[cand_ep]
            if label:
                assert reference_truth == candidate_truth
                row_evidence: dict[str, Any] = {
                    "label": True,
                    "equivalence_proof": {"goal": "Iff reference candidate", "check": check},
                    "source_proof": evidence["source_proof"],
                    "reference_truth": reference_truth,
                    "candidate_truth": candidate_truth,
                }
            else:
                assert reference_truth != candidate_truth
                row_evidence = {
                    "label": False,
                    "refutation": {"goal": "Not (Iff reference candidate)", "check": check},
                    "source_proof": evidence["source_proof"],
                    "source_proof_check": evidence["source_proof_check"],
                    "reference_truth": reference_truth,
                    "candidate_truth": candidate_truth,
                }
            row_evidence["square"] = evidence
            evidence_hash = hash_canonical(row_evidence)
            row_hash = hash_canonical(
                {
                    "root_id": root_id,
                    "operation_id": operation_id,
                    "row_kind": kind,
                    "reference_expr_hash": ref_hash,
                    "candidate_expr_hash": cand_hash,
                    "label": label,
                    "evidence_hash": evidence_hash,
                    "spec_hash": reference["spec_hash"],
                    "implementation_identity": reference["implementation_identity"],
                }
            )
            sidecar = {
                "pair_id": pair_id,
                "root_id": root_id,
                "group_id": root_id,
                "root_name": name,
                "module": record.get("module"),
                "statement": self.statements.get(name),
                "operation_id": operation_id,
                "mechanism": engine_module.mechanism_of(operation_id),
                "row_kind": kind,
                "row_schema": ROW_SCHEMA,
                "label": label,
                "orientation": "square_fixed",
                "core_family": core_family,
                "core_cell": kind,
                "square": {
                    "direction": direction,
                    "alpha": dict(cast(Mapping[str, Any], record.get("alpha") or {})),
                    "alpha_reconciliation": dict(reconciliation) if reconciliation else None,
                    "negative_operation": evidence.get("negative_operation")
                    or SQUARE_OPERATIONS[operation_id]["negative"],
                    "t_p": evidence["t_p"],
                    "t_c": evidence["t_c"],
                    "site_p": evidence.get("site_p"),
                    "site_c": evidence.get("site_c"),
                    "reference_endpoint": ref_ep,
                    "candidate_endpoint": cand_ep,
                    "source_run": root.get("source_run"),
                    "source_pair_id": root.get("source_pair_id"),
                },
                "site": {"kind": "square", "detail": direction},
                "evidence": row_evidence,
                "evidence_hash": evidence_hash,
                "reference_truth": reference_truth,
                "candidate_truth": candidate_truth,
                "repr": {
                    "reference": reference,
                    "candidate": candidate,
                    "reference_source_material": cast(dict[str, Any], render[ref_ep])[
                        "source_material"
                    ],
                    "candidate_source_material": cast(dict[str, Any], render[cand_ep])[
                        "source_material"
                    ],
                },
                "project": self.base.pins.to_dict(),
                # the engine that generated the certificates, not the engine building the rows
                "engine": record.get("engine") or self.base.identity.to_dict(),
                "cache": cache_block,
                "lean_request_hashes": {
                    "process": record.get("process_request_hash"),
                    "render": render.get("request_hash"),
                },
                "level_params": record.get("level_params"),
                "implementation_commit": record.get("implementation_commit"),
                "implementation_commit_source": record.get(
                    "implementation_commit_source", "cache_record"
                ),
                "runner_source_sha256": hash_file(Path(__file__)),
                "cache_schema": 2,
                "proof_check_time": "original_generation",
            }
            if str(reference["rendered_goal_hash"]) != render_hash(ref_text):
                raise SquareError("reference render hash disagrees with its text")
            rows.append(
                {
                    "row": {"reference": ref_text, "candidate": cand_text, "label": label},
                    "sidecar": sidecar,
                    "row_hash": row_hash,
                    "unordered_pair_key": unordered_pair_key(
                        str(reference["rendered_goal_hash"]), str(candidate["rendered_goal_hash"])
                    ),
                    "label": label,
                    "operation_id": operation_id,
                    "root_name": name,
                    "mechanism": engine_module.mechanism_of(SQUARE_OPERATION),
                }
            )
        return rows

    # ---------------------------------------------------------------- status

    def write_status(self, *, final: bool, replay: bool = False) -> dict[str, Any]:
        wall = time.monotonic() - self.started
        session = self.base.session
        summary = {
            "run_id": self.run_id,
            "operation_id": self.operation_id,
            "updated_at": utc_now(),
            "roots_considered": self.lean_roots + self.cache_roots,
            "roots_lean": self.lean_roots,
            "roots_cache": self.cache_roots,
            "retained_roots": self.retained,
            "rows": self.retained * 4,
            "terminals_by_status": dict(self.counts),
            "lean_requests": session.request_count if session else 0,
            "lean_elapsed_ms": session.lean_elapsed_ms if session else 0,
            "wall_seconds": round(wall, 3),
            "batches": self.batches,
            "peak_process_tree_rss_bytes": self.base.rss.sample(),
            "final": final,
            "replay_mode": replay,
        }
        target = self.paths.run_dir / "replay_status.json" if replay else self.paths.status
        write_atomic(target, canonical_json_bytes(summary) + b"\n")
        return summary


class Wave4Runner(SquareRunner):
    """All-site, bounded-composition runner using the existing journal/cache/session stack."""

    def __init__(
        self,
        repo_root: Path,
        loaded: LoadedConfig[SprintConfig],
        *,
        policy: OrbitPolicy,
        run_id: str,
        roots: Sequence[Mapping[str, Any]],
        operation_id: str,
        maximum_depth: int | None = None,
        max_roots: int | None = None,
        owner_session: str = "codex-sft1-wave4",
        use_cache: bool = True,
        isolated_cache: bool = False,
    ) -> None:
        if operation_id not in WAVE4_OPERATIONS:
            raise OrbitError(f"{operation_id!r} is not a Wave 4 orbit operation")
        self.policy = policy
        self.maximum_depth = maximum_depth or policy.maximum_depth
        if not 1 <= self.maximum_depth <= policy.maximum_depth:
            raise OrbitError("Wave 4 run depth is outside the policy bound")
        super().__init__(
            repo_root,
            loaded,
            run_id=run_id,
            roots=roots,
            max_roots=max_roots,
            owner_session=owner_session,
            use_cache=use_cache,
            operation_id=operation_id,
            cache_schema=WAVE4_CACHE_SCHEMA,
            isolated_cache=isolated_cache,
        )
        self.retained_rows = 0
        self.retained_variants = 0

    def square_root_key(self, name: str) -> str:
        return wave4_cache_key(
            operation_id=self.operation_id,
            name=name,
            policy_hash=self.policy.policy_hash,
            maximum_depth=self.maximum_depth,
            engine_source_sha256=self.base.identity.source_sha256,
            compile_context_id=self.base.context.compile_context_id,
            engine_semantic_version=self.base.identity.semantic_version,
            project_revision=self.base.pins.project_revision,
            lean_version=self.base.pins.lean_version,
            import_options_fingerprint=self.base.identity.import_options_fingerprint,
            revision=operation_cache_revision(self.operation_id),
        )

    def manifest_identity(self) -> dict[str, Any]:
        identity = super().manifest_identity()
        identity.update(
            {
                "runner_kind": WAVE4_CACHE_KIND,
                "wave4_cache_schema": WAVE4_CACHE_SCHEMA,
                "wave4_policy_hash": self.policy.policy_hash,
                "wave4_maximum_depth": self.maximum_depth,
            }
        )
        return identity

    def write_run_manifest(self, *, replay: bool = False) -> None:
        identity = self.manifest_identity()
        if self.paths.run_manifest.is_file():
            recorded = read_json_object(self.paths.run_manifest)
            mismatches = [
                f"{key}: manifest {recorded.get(key)!r} != current {value!r}"
                for key, value in identity.items()
                if recorded.get(key) != value
            ]
            if mismatches:
                raise SquareError(
                    f"Wave 4 run {self.run_id!r} cannot resume: " + "; ".join(mismatches)
                )
            return
        manifest = {
            "schema_version": 1,
            "sprint_id": self.config.sprint_id,
            "run_id": self.run_id,
            "started_at": utc_now(),
            **identity,
            "policy": self.policy.payload(),
            "project": self.base.pins.to_dict(),
            "engine": self.base.identity.to_dict(),
            "implementation_commit": _git(self.repo_root, "rev-parse", "HEAD"),
            "implementation_dirty": bool(_git(self.repo_root, "status", "--porcelain")),
            "max_roots": self.max_roots,
            "cache_root": str(self.cache.root),
            "replay_requested": replay,
            "argv": sys.argv,
        }
        write_atomic(self.paths.run_manifest, canonical_json_bytes(manifest) + b"\n")

    def recover_in_flight(self, begun: Mapping[str, Mapping[str, Any]]) -> None:
        present_pairs: dict[str, set[str]] = {}
        present_groups: dict[str, set[str]] = {}
        wanted = set(begun)
        for item in read_retained(self.paths.retained):
            sidecar = cast(dict[str, Any], item["sidecar"])
            root = str(sidecar.get("root_name"))
            if root not in wanted:
                continue
            present_pairs.setdefault(root, set()).add(str(sidecar.get("pair_id")))
            present_groups.setdefault(root, set()).update(
                str(value) for value in sidecar.get("closure_group_ids", [])
            )
        for root, record in begun.items():
            expected_pairs = {str(value) for value in record.get("pair_ids", [])}
            logical_groups = cast(list[dict[str, Any]], record.get("logical_groups") or [])
            expected_groups = {str(value.get("group_id")) for value in logical_groups}
            complete = (
                bool(expected_pairs)
                and bool(expected_groups)
                and expected_pairs <= present_pairs.get(root, set())
                and expected_groups <= present_groups.get(root, set())
            )
            if complete:
                self.journal.append(
                    {
                        "kind": "square_terminal",
                        "root": root,
                        "status": "retained",
                        "reason": "",
                        "source": "recovered",
                        "batch": int(record.get("batch", 0)),
                        "pair_ids": sorted(expected_pairs),
                        "logical_groups": logical_groups,
                    }
                )
                self.done[root] = "retained"
                self.counts["retained"] = self.counts.get("retained", 0) + 1
                self.retained += 1
                self.lean_roots += 1
            else:
                self.journal.append(
                    {
                        "kind": "square_abandoned",
                        "root": root,
                        "pair_ids": sorted(expected_pairs),
                        "group_ids": sorted(expected_groups),
                    }
                )

    def load_state(self) -> None:
        super().load_state()
        self.retained_rows = 0
        self.retained_variants = 0
        seen: set[str] = set()
        for record in self.journal.read():
            if record.get("kind") != "square_terminal" or record.get("status") != "retained":
                continue
            root = str(record.get("root"))
            if root in seen:
                continue
            seen.add(root)
            self.retained_rows += len({str(value) for value in record.get("pair_ids", [])})
            self.retained_variants += len(record.get("logical_groups") or [])

    def _cache_record(
        self,
        name: str,
        payload: Mapping[str, Any],
        process_request_hash: str,
        *,
        selected: Sequence[Mapping[str, Any]] = (),
        render_request_hash: str | None = None,
    ) -> dict[str, Any]:
        normalized_payload = dict(payload)
        elapsed_ms = normalized_payload.pop("elapsed_ms", None)
        return {
            "schema_version": 1,
            "kind": WAVE4_CACHE_KIND,
            "cache_schema": WAVE4_CACHE_SCHEMA,
            "operation_id": self.operation_id,
            "operation_revision": operation_cache_revision(self.operation_id),
            "root": name,
            "status": normalized_payload.get("status"),
            "reason": normalized_payload.get("reason", ""),
            "policy_hash": self.policy.policy_hash,
            "maximum_depth": self.maximum_depth,
            "payload": normalized_payload,
            "enumeration_hash": (
                normalized_payload.get("enumeration_hash")
                or hash_canonical(normalized_payload.get("variants"))
                if normalized_payload.get("status") == "retained"
                else None
            ),
            "selected": [dict(item) for item in selected],
            "engine": self.base.identity.to_dict(),
            "implementation_commit": self.base.implementation_commit,
            "process_request_hash": process_request_hash,
            "render_request_hash": render_request_hash,
            "elapsed_ms": elapsed_ms,
        }

    @staticmethod
    def _cache_semantic_content(record: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in record.items()
            if key not in {"elapsed_ms", "implementation_commit"}
        }

    def _cache_put_record(self, name: str, record: Mapping[str, Any]) -> Mapping[str, Any]:
        key = self.square_root_key(name)
        existing = self.cache.get_root(key)
        if existing is None:
            self.cache.put_root(key, record)
            return record
        if self._cache_semantic_content(existing) != self._cache_semantic_content(record):
            raise SquareError(f"conflicting Wave 4 cache record for root {name!r}")
        return existing

    def try_cache(self, root: Mapping[str, Any]) -> bool:
        if not self.use_cache:
            return False
        name = str(root["name"])
        record = self.cache.get_root(self.square_root_key(name))
        if record is None or not cacheable_status(record.get("status")):
            return False
        if record.get("policy_hash") != self.policy.policy_hash:
            raise SquareError(f"Wave 4 cache policy mismatch for {name!r}")
        if record.get("maximum_depth") != self.maximum_depth:
            raise SquareError(f"Wave 4 cache depth mismatch for {name!r}")
        if record.get("status") == "retained":
            payload = _wave4_mapping(record.get("payload"), "wave4.cache.payload")
            descriptors = preselect_wave4_variant_descriptors(
                payload,
                operation_id=self.operation_id,
                policy=self.policy,
                maximum_depth=self.maximum_depth,
                expected_root=name,
                selection_root_id=self.base.root_id(name),
            )
            validated = validate_wave4_root_payload(
                payload,
                operation_id=self.operation_id,
                policy=self.policy,
                maximum_depth=self.maximum_depth,
                expected_root=name,
                selected_descriptors=descriptors,
                selection_root_id=self.base.root_id(name),
            )
            selected = select_wave4_variants(validated, self.policy)
            stored_selected = _wave4_sequence(record.get("selected"), "wave4.cache.selected")
            if [variant.selection_hash for variant in selected] != [
                _wave4_mapping(item, "wave4.cache.selected.item").get("selection_hash")
                for item in stored_selected
            ]:
                raise SquareError(f"Wave 4 cached selection mismatch for {name!r}")
            if not record.get("render_request_hash"):
                return False
        self.finalize(name, record, source="cache", root=root)
        return True

    def _screen_variant(self, variant: ValidatedWave4Variant) -> None:
        goals = _wave4_mapping(variant.raw["goals"], "wave4.variant.goals")
        for endpoint in ("p", "c", "p_prime", "c_prime"):
            surface, violation = canonical_surface(str(goals[endpoint]))
            if surface is None:
                raise OrbitError(f"screen_{endpoint}:{violation}")
            if self.base.gold.hit(surface):
                raise OrbitError(f"screen_{endpoint}:gold_blocklist")

    def process_batch(self, batch: Sequence[Mapping[str, Any]]) -> None:
        session = self.base.open_session()
        self.batches += 1
        names = [str(item["name"]) for item in batch]
        request = LeanRequest(
            request_id=f"{self.run_id}:wave4:{self.batches}:" + hash_canonical(names)[:16],
            context_id=self.base.context.compile_context_id,
            code=engine_module.command_text(
                self.base.context,
                wave4_process_body(names, self.operation_id, self.maximum_depth),
            ),
            allow_sorry=False,
            timeout_seconds=self.config.execution.request_timeout_seconds,
            metadata={
                "sprint_phase": "wave4_complete_root_enumeration",
                "policy_hash": self.policy.policy_hash,
                "maximum_depth": str(self.maximum_depth),
            },
        )
        result = session.backend.run(request)
        session.request_count += 1
        session.lean_elapsed_ms += result.elapsed_ms
        payloads: dict[str, dict[str, Any]] = {}
        if result.status in {LeanStatus.VALID, LeanStatus.INVALID}:
            for payload in parse_evidence_lines(result.messages):
                if payload.get("kind") == "wave4_descriptor_root":
                    payloads[str(payload.get("root"))] = payload
        missing = [name for name in names if name not in payloads]
        retryable_infrastructure = result.status in {
            LeanStatus.CRASH,
            LeanStatus.INTERNAL_ERROR,
            LeanStatus.TIMEOUT,
        }
        valid_partial_payload = result.status == LeanStatus.VALID and bool(missing)
        if len(batch) > 1 and (retryable_infrastructure or valid_partial_payload):
            half = len(batch) // 2
            self.process_batch(batch[:half])
            self.process_batch(batch[half:])
            return
        for root in batch:
            name = str(root["name"])
            root_payload = payloads.get(name)
            if root_payload is None:
                errors = [
                    str(message.get("data", ""))[:300]
                    for message in result.messages
                    if str(message.get("severity", "")) == "error"
                ]
                failure = {
                    "kind": "wave4_descriptor_root",
                    "operation_id": self.operation_id,
                    "root": name,
                    "status": "error",
                    "reason": f"request_{result.status.value}:{'; '.join(errors[:2])}",
                }
                record = self._cache_record(name, failure, result.request_hash)
                self.finalize(name, record, source="lean", root=root)
                continue
            if root_payload.get("status") != "described":
                record = self._cache_record(name, root_payload, result.request_hash)
                if cacheable_status(root_payload.get("status")):
                    record = dict(self._cache_put_record(name, record))
                self.finalize(name, record, source="lean", root=root)
                continue
            try:
                descriptors = preselect_wave4_variant_descriptors(
                    root_payload,
                    operation_id=self.operation_id,
                    policy=self.policy,
                    maximum_depth=self.maximum_depth,
                    expected_root=name,
                    selection_root_id=self.base.root_id(name),
                )
                record = self._render_root(
                    root,
                    root_payload,
                    descriptors,
                    result.request_hash,
                )
            except (GoalV1Error, OrbitError, ValueError) as exc:
                rejected = dict(root_payload)
                rejected["status"] = "rejected"
                rejected["reason"] = f"wave4_validation:{str(exc)[:400]}"
                record = self._cache_record(name, rejected, result.request_hash)
                record = dict(self._cache_put_record(name, record))
            self.finalize(name, record, source="lean", root=root)
        self.write_status(final=False)

    def _render_root(
        self,
        root: Mapping[str, Any],
        descriptor_payload: Mapping[str, Any],
        selected_descriptors: Sequence[Wave4VariantDescriptor],
        process_request_hash: str,
    ) -> dict[str, Any]:
        name = str(root["name"])
        session = self.base.open_session()
        scope = self.base.scope + ":wave4"
        request_id = (
            f"{self.run_id}:wave4_render:"
            + hash_canonical(
                [name, [descriptor.selection_hash for descriptor in selected_descriptors]]
            )[:20]
        )
        batch = render_closed_expr_in_session(
            session.backend,
            inputs=wave4_render_inputs(name, len(selected_descriptors), self.statements),
            compile_context=self.base.context,
            render_scope_id=scope,
            session_body=wave4_render_body(
                name,
                [descriptor.index for descriptor in selected_descriptors],
                scope,
                self.operation_id,
                self.maximum_depth,
            ),
            request_id=request_id,
            timeout_seconds=self.config.execution.request_timeout_seconds,
        )
        session.request_count += 1
        session.lean_elapsed_ms += batch.elapsed_ms
        if batch.failures:
            detail = "; ".join(
                f"{failure.endpoint_id}: {failure.detail}" for failure in batch.failures
            )[:400]
            raise GoalV1Error(f"Wave 4 frozen render failed: {detail}")
        selected_report = _wave4_selected_report(batch.raw_response_path, root=name)
        payload = combine_wave4_selected_payload(
            descriptor_payload,
            selected_report,
            expected_indices=[descriptor.index for descriptor in selected_descriptors],
        )
        validated = validate_wave4_root_payload(
            payload,
            operation_id=self.operation_id,
            policy=self.policy,
            maximum_depth=self.maximum_depth,
            expected_root=name,
            selected_descriptors=selected_descriptors,
            selection_root_id=self.base.root_id(name),
        )
        selected = select_wave4_variants(validated, self.policy)
        if [variant.index for variant in selected] != [
            descriptor.index for descriptor in selected_descriptors
        ]:
            raise OrbitError("Wave 4 selected certificate order differs from preselection")
        for variant in selected:
            self._screen_variant(variant)
        payload["enumeration_hash"] = validated.enumeration_hash
        sidecars = {sidecar.record.endpoint_id: sidecar for sidecar in batch.sidecars}
        selected_records: list[dict[str, Any]] = []
        for slot, variant in enumerate(selected):
            endpoints: dict[str, Any] = {}
            goals = _wave4_mapping(variant.raw["goals"], "wave4.variant.goals")
            for endpoint in ("p", "c", "p_prime", "c_prime"):
                sidecar = sidecars.get(f"{slot}.{endpoint}")
                if sidecar is None:
                    raise GoalV1Error(f"Wave 4 render omitted {slot}.{endpoint}")
                expected, violation = canonical_surface(str(goals[endpoint]))
                if expected is None or sidecar.core_text() != expected:
                    raise GoalV1Error(
                        f"Wave 4 render mismatch at {slot}.{endpoint}: {violation or 'text'}"
                    )
                endpoints[endpoint] = {
                    "record": sidecar.record.to_dict(),
                    "source_material": sidecar.source_material.to_dict(),
                }
            selected_records.append(
                {
                    "index": variant.index,
                    "selection_hash": variant.selection_hash,
                    "content_hash": variant.content_hash,
                    "reference_chain_hash": variant.reference_chain_hash,
                    "candidate_chain_hash": variant.candidate_chain_hash,
                    "reference_site_hash": variant.reference_site_hash,
                    "candidate_site_hash": variant.candidate_site_hash,
                    "variant": variant.raw,
                    "render": endpoints,
                }
            )
        record = self._cache_record(
            name,
            payload,
            process_request_hash,
            selected=selected_records,
            render_request_hash=batch.request_hash,
        )
        return dict(self._cache_put_record(name, record))

    @staticmethod
    def _row_evidence(
        row_kind: str,
        evidence: Mapping[str, Any],
        selection_hash: str,
    ) -> tuple[dict[str, Any], Mapping[str, Any]]:
        negative_certificate = evidence["base_candidate_refutation"]
        if row_kind == "preserving_reference":
            check = _wave4_mapping(evidence["p_composite_iff"], "p_composite_iff")
            payload = {
                "label": True,
                "equivalence_proof": {"goal": "Iff candidate reference", "check": check},
                "transported_source_proof": evidence["p_prime_transported_proof"],
                "selection_hash": selection_hash,
                "closure": evidence,
            }
        elif row_kind == "preserving_candidate":
            check = _wave4_mapping(evidence["c_composite_iff"], "c_composite_iff")
            payload = {
                "label": True,
                "equivalence_proof": {"goal": "Iff reference candidate", "check": check},
                "candidate_refutation": evidence["c_prime_refutation"],
                "selection_hash": selection_hash,
                "closure": evidence,
            }
        elif row_kind == "negative_base":
            check = _wave4_mapping(evidence["not_iff_c_p"], "not_iff_c_p")
            payload = {
                "label": False,
                "refutation": {"goal": "Not (Iff reference candidate)", "check": check},
                "source_proof": evidence["source_proof"],
                "source_proof_check": evidence["source_proof_check"],
                "base_candidate_refutation": negative_certificate,
                "negative_family_evidence": negative_certificate,
            }
        elif row_kind == "negative_last":
            check = _wave4_mapping(evidence["not_iff_p_prime_c_prime"], "not_iff_p_prime_c_prime")
            payload = {
                "label": False,
                "refutation": {"goal": "Not (Iff reference candidate)", "check": check},
                "transported_source_proof": evidence["p_prime_transported_proof"],
                "candidate_refutation": evidence["c_prime_refutation"],
                "negative_last_replay": evidence["negative_last_replay"],
                "negative_family_evidence": negative_certificate,
                "selection_hash": selection_hash,
                "closure": evidence,
            }
        else:
            raise OrbitError(f"unknown Wave 4 row kind {row_kind!r}")
        return payload, check

    @staticmethod
    def _render_endpoint(
        render: Mapping[str, Any], endpoint: str, *, shared_base: bool
    ) -> Mapping[str, Any]:
        """Return a frozen-render block with a stable physical endpoint identity.

        The render request addresses ``p`` and ``c`` through a selected-variant slot.
        Those slots are generation details, not distinct base edges, so the one shared
        negative-base row receives canonical endpoint IDs.  Variant rows keep their
        exact slot IDs in their own sidecars.
        """

        block = dict(_wave4_mapping(render.get(endpoint), f"render.{endpoint}"))
        record = dict(_wave4_mapping(block.get("record"), f"render.{endpoint}.record"))
        if shared_base:
            record["endpoint_id"] = f"base.{endpoint}"
            if "representation_id" in record:
                identity_fields = (
                    "renderer_version",
                    "spec_hash",
                    "goal_v1_source",
                    "goal_v1",
                    "rendered_goal_hash",
                    "endpoint_id",
                    "endpoint_role",
                    "source_material_hash",
                    "compile_context_id",
                    "provenance",
                    "implementation_identity",
                )
                missing = [field for field in identity_fields if field not in record]
                if missing:
                    raise OrbitError(
                        "shared Wave 4 base endpoint cannot replay its representation identity: "
                        + ", ".join(missing)
                    )
                record["representation_id"] = "repr:" + hash_canonical(
                    {field: record[field] for field in identity_fields}
                )
        block["record"] = record
        return block

    def build_wave4_rows(
        self, name: str, record: Mapping[str, Any], root: Mapping[str, Any]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        selected = _wave4_sequence(record.get("selected"), "wave4.record.selected")
        if not selected:
            raise OrbitError("retained Wave 4 cache record has no selected variants")
        root_id = self.base.root_id(name)
        negative_operation = SQUARE_OPERATIONS[self.operation_id]["negative"]
        negative_mechanism = engine_module.mechanism_of(negative_operation)
        cache_key = self.square_root_key(name)
        cache_block = {
            "kind": WAVE4_CACHE_KIND,
            "schema": WAVE4_CACHE_SCHEMA,
            "revision": operation_cache_revision(self.operation_id),
            "key": cache_key,
            "path": f"roots/{cache_key[:2]}/{cache_key}.json",
            "content_sha256": hash_canonical(record),
            "snapshot": None,
        }
        logical: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        group_records: list[dict[str, Any]] = []
        for item_index, raw_item in enumerate(selected):
            item = _wave4_mapping(raw_item, f"wave4.record.selected[{item_index}]")
            selection_hash = _wave4_text(item.get("selection_hash"), "selection_hash")
            variant = _wave4_mapping(item.get("variant"), "wave4.selected.variant")
            evidence = _wave4_mapping(variant.get("evidence"), "wave4.selected.variant.evidence")
            hops = [
                _wave4_mapping(value, "wave4.selected.variant.evidence.hop")
                for value in _wave4_sequence(
                    evidence.get("hops"), "wave4.selected.variant.evidence.hops"
                )
            ]
            render = _wave4_mapping(item.get("render"), "wave4.selected.render")
            group_id = make_id(
                VARIANT_PREFIX,
                {
                    "kind": "sft1_wave4_certificate_closure_v1",
                    "root_id": root_id,
                    "operation_id": self.operation_id,
                    "negative_operation": negative_operation,
                    "selection_hash": selection_hash,
                    "content_hash": item.get("content_hash"),
                },
            )
            group_pairs: dict[str, str] = {}
            for row_kind, label, ref_ep, cand_ep, _ in WAVE4_ROW_KINDS:
                shared_base = row_kind == "negative_base"
                reference_block = self._render_endpoint(render, ref_ep, shared_base=shared_base)
                candidate_block = self._render_endpoint(render, cand_ep, shared_base=shared_base)
                reference = _wave4_mapping(reference_block.get("record"), "reference.record")
                candidate = _wave4_mapping(candidate_block.get("record"), "candidate.record")
                ref_text = _wave4_text(reference.get("goal_v1"), "reference.goal_v1")
                cand_text = _wave4_text(candidate.get("goal_v1"), "candidate.goal_v1")
                ref_hash = _wave4_text(
                    _wave4_mapping(reference.get("provenance"), "reference.provenance").get(
                        "expr_hash"
                    ),
                    "reference.expr_hash",
                )
                cand_hash = _wave4_text(
                    _wave4_mapping(candidate.get("provenance"), "candidate.provenance").get(
                        "expr_hash"
                    ),
                    "candidate.expr_hash",
                )
                row_evidence, check = self._row_evidence(row_kind, evidence, selection_hash)
                evidence_hash = hash_canonical(row_evidence)
                if row_kind == "negative_base":
                    chain_hash = hash_canonical(
                        {
                            "kind": "sft1_wave4_base_negative_chain_v1",
                            "negative_operation": negative_operation,
                        }
                    )
                    site_hash = hash_canonical(
                        {
                            "direction": evidence.get("direction"),
                            "base_candidate_refutation": evidence.get("base_candidate_refutation"),
                        }
                    )
                elif row_kind == "preserving_reference":
                    chain_hash = str(item["reference_chain_hash"])
                    site_hash = str(item["reference_site_hash"])
                elif row_kind == "preserving_candidate":
                    chain_hash = str(item["candidate_chain_hash"])
                    site_hash = str(item["candidate_site_hash"])
                else:
                    chain_hash = hash_canonical(
                        [item["reference_chain_hash"], item["candidate_chain_hash"]]
                    )
                    site_hash = hash_canonical(
                        [item["reference_site_hash"], item["candidate_site_hash"]]
                    )
                pair_id = make_id(
                    PAIR_PREFIX,
                    {
                        "kind": "sft1_wave4_physical_pair_v1",
                        "root_id": root_id,
                        "operation_id": self.operation_id,
                        "negative_operation": negative_operation,
                        "row_kind": row_kind,
                        "reference_expr_hash": ref_hash,
                        "candidate_expr_hash": cand_hash,
                        "label": label,
                        "operation_chain_hash": chain_hash,
                        "selected_site_hash": site_hash,
                        "evidence_hash": evidence_hash,
                    },
                )
                group_pairs[row_kind] = pair_id
                sidecar = {
                    "pair_id": pair_id,
                    "root_id": root_id,
                    "root_name": name,
                    "module": cast(Mapping[str, Any], record.get("payload") or {}).get("module"),
                    "statement": self.statements.get(name),
                    "operation_id": self.operation_id,
                    "negative_operation": negative_operation,
                    "mechanism": negative_mechanism,
                    "row_kind": row_kind,
                    "row_schema": ROW_SCHEMA,
                    "label": label,
                    "orientation": "wave4_certificate_closure",
                    "core_family": SQUARE_OPERATIONS[self.operation_id]["family"],
                    "core_cell": row_kind,
                    "closure_group_ids": [group_id],
                    "wave4": (
                        {
                            "logical_role": "negative_base",
                            "negative_operation": negative_operation,
                            "base_negative_evidence_hash": hash_canonical(
                                evidence.get("base_candidate_refutation")
                            ),
                        }
                        if shared_base
                        else {
                            "selection_hash": selection_hash,
                            "content_hash": item.get("content_hash"),
                            "enumeration_hash": record.get("enumeration_hash"),
                            "variant_index": item.get("index"),
                            "depth": variant.get("depth"),
                            "reference_chain_hash": item.get("reference_chain_hash"),
                            "candidate_chain_hash": item.get("candidate_chain_hash"),
                            "reference_site_hash": item.get("reference_site_hash"),
                            "candidate_site_hash": item.get("candidate_site_hash"),
                            "logical_role": row_kind,
                        }
                    ),
                    "site": {"kind": "wave4_chain", "detail": site_hash},
                    "evidence": row_evidence,
                    "evidence_hash": evidence_hash,
                    "repr": {
                        "reference": reference,
                        "candidate": candidate,
                        "reference_source_material": reference_block.get("source_material"),
                        "candidate_source_material": candidate_block.get("source_material"),
                    },
                    "project": self.base.pins.to_dict(),
                    "engine": record.get("engine") or self.base.identity.to_dict(),
                    "cache": cache_block,
                    "lean_request_hashes": {
                        "process": record.get("process_request_hash"),
                        "render": record.get("render_request_hash"),
                    },
                    "level_params": cast(Mapping[str, Any], record.get("payload") or {}).get(
                        "level_params"
                    ),
                    "implementation_commit": record.get("implementation_commit"),
                    "runner_source_sha256": hash_file(Path(__file__)),
                    "proof_check_time": "original_generation",
                    "row_check": check,
                }
                logical.append(
                    (
                        group_id,
                        {
                            "row": {
                                "reference": ref_text,
                                "candidate": cand_text,
                                "label": label,
                            },
                            "sidecar": sidecar,
                            "unordered_pair_key": unordered_pair_key(
                                str(reference["rendered_goal_hash"]),
                                str(candidate["rendered_goal_hash"]),
                            ),
                            "label": label,
                            "operation_id": self.operation_id,
                            "root_name": name,
                            "mechanism": negative_mechanism,
                        },
                        sidecar,
                    )
                )
            group_records.append(
                {
                    "schema_version": 1,
                    "group_id": group_id,
                    "root_id": root_id,
                    "operation_id": self.operation_id,
                    "negative_operation": negative_operation,
                    "negative_mechanism": negative_mechanism,
                    "selection_hash": selection_hash,
                    "content_hash": item.get("content_hash"),
                    "depth": variant.get("depth"),
                    "reference_chain_hash": item.get("reference_chain_hash"),
                    "candidate_chain_hash": item.get("candidate_chain_hash"),
                    "reference_site_hash": item.get("reference_site_hash"),
                    "candidate_site_hash": item.get("candidate_site_hash"),
                    "reference_operation_chain": [str(hop["p_operation"]) for hop in hops],
                    "candidate_operation_chain": [str(hop["c_operation"]) for hop in hops],
                    "preserving_mechanism_chain": [str(hop["mechanism"]) for hop in hops],
                    "preserving_superclass_chain": [str(hop["superclass"]) for hop in hops],
                    "base_negative_evidence_hash": hash_canonical(
                        evidence.get("base_candidate_refutation")
                    ),
                    "negative_last_replay_hash": hash_canonical(
                        evidence.get("negative_last_replay")
                    ),
                    "logical_pair_ids": group_pairs,
                    "closure_certificate_hash": hash_canonical(evidence.get("closure")),
                }
            )

        physical: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        memberships: dict[str, set[str]] = {}
        unordered: dict[str, tuple[str, bool]] = {}
        for group_id, item, sidecar in logical:
            pair_id = str(sidecar["pair_id"])
            memberships.setdefault(pair_id, set()).add(group_id)
            previous = physical.get(pair_id)
            if previous is not None:
                if sidecar["row_kind"] != "negative_base":
                    raise OrbitError("only the exact Wave 4 base negative may be shared")
                previous_item, previous_sidecar = previous
                comparable = dict(sidecar)
                comparable["closure_group_ids"] = previous_sidecar["closure_group_ids"]
                if comparable != previous_sidecar or item["row"] != previous_item["row"]:
                    raise OrbitError("shared Wave 4 base edge has conflicting content")
                continue
            owner = unordered.get(str(item["unordered_pair_key"]))
            if owner is not None and owner != (pair_id, bool(item["label"])):
                kind = "conflicting label" if owner[1] != bool(item["label"]) else "duplicate pair"
                raise OrbitError(f"Wave 4 closure has a {kind}")
            unordered[str(item["unordered_pair_key"])] = (pair_id, bool(item["label"]))
            physical[pair_id] = (item, sidecar)
        rows: list[dict[str, Any]] = []
        for pair_id in sorted(physical):
            item, sidecar = physical[pair_id]
            sidecar["closure_group_ids"] = sorted(memberships[pair_id])
            item["row_hash"] = hash_canonical(
                {
                    "kind": "sft1_wave4_retained_row_v1",
                    "row": item["row"],
                    "pair_id": pair_id,
                    "evidence_hash": sidecar["evidence_hash"],
                    "closure_group_ids": sidecar["closure_group_ids"],
                }
            )
            rows.append(item)
        physical_ids = set(physical)
        for group in group_records:
            pair_ids = set(cast(Mapping[str, str], group["logical_pair_ids"]).values())
            if len(pair_ids) != 4 or not pair_ids <= physical_ids:
                raise OrbitError("Wave 4 logical closure group is partial")
        return rows, group_records

    def finalize(
        self, name: str, record: Mapping[str, Any], *, source: str, root: Mapping[str, Any]
    ) -> None:
        status = str(record.get("status"))
        if status != "retained":
            super().finalize(name, record, source=source, root=root)
            return
        rows, groups = self.build_wave4_rows(name, record, root)
        begin = {
            "kind": "square_begin",
            "root": name,
            "batch": self.batches,
            "pair_ids": [str(item["sidecar"]["pair_id"]) for item in rows],
            "row_hashes": [str(item["row_hash"]) for item in rows],
            "logical_groups": groups,
        }
        self.journal.append(begin)
        with self.paths.retained.open("ab") as handle:
            for row in rows:
                handle.write(canonical_json_bytes(row) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        self.journal.append(
            {
                "kind": "square_terminal",
                "root": name,
                "status": "retained",
                "reason": "",
                "source": source,
                "batch": self.batches,
                "pair_ids": begin["pair_ids"],
                "logical_groups": groups,
            }
        )
        self.done[name] = "retained"
        self.counts["retained"] = self.counts.get("retained", 0) + 1
        self.retained += 1
        self.retained_rows += len(rows)
        self.retained_variants += len(groups)
        if source == "cache":
            self.cache_roots += 1
        else:
            self.lean_roots += 1

    def write_status(self, *, final: bool, replay: bool = False) -> dict[str, Any]:
        wall = time.monotonic() - self.started
        session = self.base.session
        summary = {
            "run_id": self.run_id,
            "runner_kind": WAVE4_CACHE_KIND,
            "operation_id": self.operation_id,
            "policy_hash": self.policy.policy_hash,
            "maximum_depth": self.maximum_depth,
            "updated_at": utc_now(),
            "roots_considered": self.lean_roots + self.cache_roots,
            "roots_lean": self.lean_roots,
            "roots_cache": self.cache_roots,
            "retained_roots": self.retained,
            "retained_variants": self.retained_variants,
            "logical_rows": self.retained_variants * 4,
            "physical_rows": self.retained_rows,
            "terminals_by_status": dict(self.counts),
            "lean_requests": session.request_count if session else 0,
            "lean_elapsed_ms": session.lean_elapsed_ms if session else 0,
            "wall_seconds": round(wall, 3),
            "batches": self.batches,
            "peak_process_tree_rss_bytes": self.base.rss.sample(),
            "final": final,
            "replay_mode": replay,
        }
        target = self.paths.run_dir / "replay_status.json" if replay else self.paths.status
        write_atomic(target, canonical_json_bytes(summary) + b"\n")
        return summary


# ------------------------------------------------------------------ view + gate


# ------------------------------------------------------------------ retained evidence
def terminal_pair_ids(journal_path: Path) -> dict[str, list[str]]:
    """Retained roots in journal order with the pair ids their terminal promised."""
    result: dict[str, list[str]] = {}
    if not journal_path.is_file():
        return result
    with journal_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("kind") == "square_terminal" and record.get("status") == "retained":
                result.setdefault(str(record["root"]), [str(x) for x in record.get("pair_ids", [])])
    return result


def load_square_retained(
    paths: RunPaths, retained_path: Path | None = None
) -> list[dict[str, Any]]:
    """Retained rows whose root terminal promised their pair id; terminals are the authority.

    Rows appended by a per-root transaction that never reached its terminal are orphaned
    and ignored; a pair id is kept once even if the file carries it twice.
    """
    allowed = {pair for pairs in terminal_pair_ids(paths.journal).values() for pair in pairs}
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for item in read_retained(retained_path or paths.retained):
        pair_id = str(cast(dict[str, Any], item["sidecar"]).get("pair_id"))
        if pair_id in allowed and pair_id not in seen:
            seen.add(pair_id)
            rows.append(item)
    return rows


@dataclass(frozen=True, slots=True)
class Wave4ReleaseGroup:
    """One validated logical closure group backed by unique physical rows."""

    record: dict[str, Any]

    @property
    def group_id(self) -> str:
        return str(self.record["group_id"])

    @property
    def root_id(self) -> str:
        return str(self.record["root_id"])

    @property
    def operation_id(self) -> str:
        """Negative operation ID, as required by the release share cap."""

        return str(self.record["negative_operation"])

    @property
    def mechanism(self) -> str:
        return str(self.record["negative_mechanism"])

    @property
    def row_ids(self) -> tuple[str, ...]:
        logical = cast(Mapping[str, Any], self.record["logical_pair_ids"])
        return tuple(str(logical[role]) for role in EDGE_ROLES)


@dataclass(frozen=True, slots=True)
class Wave4ClosureMaterialization:
    """Validated physical rows and their separate logical closure index."""

    rows: tuple[dict[str, Any], ...]
    groups: tuple[Wave4ReleaseGroup, ...]

    @property
    def logical_row_count(self) -> int:
        return len(self.groups) * len(EDGE_ROLES)


@dataclass(frozen=True, slots=True)
class Wave4ClosureSelection:
    """Whole-group release selection with physical-row-aware accounting."""

    materialized: Wave4ClosureMaterialization
    input_group_count: int
    input_physical_rows: int
    capacity_dropped_group_ids: tuple[str, ...]
    negative_share_report: dict[str, object]
    pair_delta_balance_report: dict[str, object] = field(default_factory=dict)


def terminal_wave4_groups(journal_path: Path) -> tuple[dict[str, Any], ...]:
    """Read the authoritative logical groups from retained root terminals."""

    groups: dict[str, dict[str, Any]] = {}
    if not journal_path.is_file():
        return ()
    with journal_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            terminal = json.loads(line)
            if terminal.get("kind") != "square_terminal" or terminal.get("status") != "retained":
                continue
            for raw_group in terminal.get("logical_groups") or []:
                group = dict(_wave4_mapping(raw_group, "wave4.terminal.logical_group"))
                group_id = _wave4_text(group.get("group_id"), "wave4.group.group_id")
                previous = groups.get(group_id)
                if previous is not None and previous != group:
                    raise SquareError(f"conflicting Wave 4 logical group {group_id}")
                groups.setdefault(group_id, group)
    return tuple(groups[group_id] for group_id in sorted(groups))


def load_wave4_retained(paths: RunPaths) -> Wave4ClosureMaterialization:
    """Load only terminal-authorized physical rows plus their logical group index."""

    return materialize_wave4_records(
        load_square_retained(paths), terminal_wave4_groups(paths.journal)
    )


def load_wave4_retained_dir(run_dir: Path) -> Wave4ClosureMaterialization:
    """Load terminal-authorized Wave 4 rows from an explicit run directory."""

    journal_path = run_dir / "journal.jsonl"
    retained_path = run_dir / "retained.jsonl"
    allowed = {
        pair_id for pair_ids in terminal_pair_ids(journal_path).values() for pair_id in pair_ids
    }
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for item in read_retained(retained_path):
        sidecar = _wave4_mapping(item.get("sidecar"), "wave4.retained.sidecar")
        pair_id = _wave4_text(sidecar.get("pair_id"), "wave4.retained.pair_id")
        if pair_id in allowed and pair_id not in seen:
            rows.append(item)
            seen.add(pair_id)
    return materialize_wave4_records(rows, terminal_wave4_groups(journal_path))


_WAVE4_RUN_FILES = {
    "manifest": "run.json",
    "status": "status.json",
    "journal": "journal.jsonl",
    "retained": "retained.jsonl",
    "replay": "replay_report.json",
}


def _wave4_run_hashes(run_dir: Path) -> dict[str, str]:
    missing = [
        filename for filename in _WAVE4_RUN_FILES.values() if not (run_dir / filename).is_file()
    ]
    if missing:
        raise SquareError(f"Wave 4 run {run_dir} is missing: {', '.join(missing)}")
    return {
        receipt_name: hash_file(run_dir / filename)
        for receipt_name, filename in _WAVE4_RUN_FILES.items()
    }


def load_completed_wave4_run(
    repo_root: Path, run_dir: Path, *, policy_hash: str
) -> tuple[dict[str, Any], Wave4ClosureMaterialization]:
    """Load one immutable, clean, replayed Wave 4 run and its exact receipt."""

    from leanfaith.sft1.sprint.integrity import git_commit_is_ancestor

    resolved = run_dir.resolve()
    before = _wave4_run_hashes(resolved)
    manifest = read_json_object(resolved / "run.json")
    status = read_json_object(resolved / "status.json")
    replay = read_json_object(resolved / "replay_report.json")
    bundle = load_wave4_retained_dir(resolved)
    after = _wave4_run_hashes(resolved)
    if before != after:
        raise SquareError(f"Wave 4 run {resolved} changed while its receipt was built")
    run_id = manifest.get("run_id")
    project = manifest.get("project")
    if not isinstance(run_id, str) or not run_id or not isinstance(project, Mapping):
        raise SquareError(f"Wave 4 run {resolved} lacks run/project identity")
    project_id = project.get("project_id")
    project_revision = project.get("project_revision")
    if not isinstance(project_id, str) or not isinstance(project_revision, str):
        raise SquareError(f"Wave 4 run {resolved} has malformed project identity")
    raw_retained = read_retained(resolved / "retained.jsonl")
    raw_pair_ids = [
        str(_wave4_mapping(item.get("sidecar"), "wave4.retained.sidecar").get("pair_id"))
        for item in raw_retained
    ]
    terminals: dict[str, int] = {}
    recovered_terminals = 0
    with (resolved / "journal.jsonl").open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            item = json.loads(line)
            if item.get("kind") == "square_terminal":
                root = str(item.get("root", ""))
                terminals[root] = terminals.get(root, 0) + 1
                recovered_terminals += int(item.get("source") == "recovered")
    replay_ok = (
        replay.get("run_id") == run_id
        and replay.get("lean_requests") == 0
        and replay.get("duplicate_rows") == 0
        and replay.get("retained_before") == len(raw_retained)
        and replay.get("retained_after") == len(raw_retained)
        and replay.get("roots_considered") == status.get("roots_considered")
    )
    checks = {
        "wave4_runner_kind": manifest.get("runner_kind") == WAVE4_CACHE_KIND,
        "common_policy_hash": manifest.get("wave4_policy_hash") == policy_hash,
        "final_status": status.get("run_id") == run_id
        and status.get("runner_kind") == WAVE4_CACHE_KIND
        and status.get("policy_hash") == policy_hash
        and status.get("final") is True,
        "terminal_roots_unique": bool(terminals)
        and all(count == 1 for count in terminals.values()),
        "terminal_authorized_rows_exact": len(raw_pair_ids) == len(set(raw_pair_ids))
        and set(raw_pair_ids)
        == {
            str(_wave4_mapping(item["sidecar"], "wave4.retained.sidecar")["pair_id"])
            for item in bundle.rows
        },
        "status_counts_match": status.get("physical_rows") == len(bundle.rows)
        and status.get("retained_variants") == len(bundle.groups),
        "zero_call_replay": replay_ok,
        "clean_generator": manifest.get("implementation_dirty") is False,
        "generator_commit_ancestor": git_commit_is_ancestor(
            repo_root, manifest.get("implementation_commit")
        ),
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise SquareError(f"Wave 4 run {resolved} is not release-authorized: {', '.join(failed)}")
    filename_hashes = {
        filename: before[receipt_name] for receipt_name, filename in _WAVE4_RUN_FILES.items()
    }
    source_key = hash_canonical([project_id, project_revision, run_id, filename_hashes])
    receipt = {
        "schema_version": 1,
        "run_dir": str(resolved),
        "run_id": run_id,
        "source_key": source_key,
        "project_id": project_id,
        "project_revision": project_revision,
        "operation_id": manifest.get("operation_id"),
        "policy_hash": policy_hash,
        "cache_root": manifest.get("cache_root"),
        "implementation_commit": manifest.get("implementation_commit"),
        "implementation_dirty": manifest.get("implementation_dirty"),
        "forced_resume_observed": recovered_terminals > 0,
        "input_sha256": before,
        "physical_rows": len(bundle.rows),
        "logical_groups": len(bundle.groups),
        "checks": checks,
        "performance": {
            field: status.get(field)
            for field in (
                "roots_considered",
                "roots_lean",
                "roots_cache",
                "lean_requests",
                "lean_elapsed_ms",
                "wall_seconds",
                "peak_process_tree_rss_bytes",
            )
        },
    }
    return receipt, bundle


def _wave4_row_hash(row: Mapping[str, Any], sidecar: Mapping[str, Any]) -> str:
    return hash_canonical(
        {
            "kind": "sft1_wave4_retained_row_v1",
            "row": row,
            "pair_id": sidecar["pair_id"],
            "evidence_hash": sidecar["evidence_hash"],
            "closure_group_ids": sidecar["closure_group_ids"],
        }
    )


def materialize_wave4_records(
    records: Sequence[Mapping[str, Any]], group_records: Sequence[Mapping[str, Any]]
) -> Wave4ClosureMaterialization:
    """Validate the shared-edge closure model used by run and release artifacts.

    Logical groups always have four roles.  Their physical union has ``3n + 1``
    rows for ``n`` variants of one base edge.  Only the byte-identical
    ``negative_base`` row may belong to more than one group.
    """

    rows_by_pair: dict[str, dict[str, Any]] = {}
    unordered: dict[str, tuple[str, bool]] = {}
    for raw_record in records:
        record = dict(raw_record)
        row = _wave4_mapping(record.get("row"), "wave4.record.row")
        if set(row) != {"reference", "candidate", "label"}:
            raise OrbitError("Wave 4 model row does not have exactly three fields")
        sidecar = _wave4_mapping(record.get("sidecar"), "wave4.record.sidecar")
        pair_id = _wave4_text(sidecar.get("pair_id"), "wave4.record.pair_id")
        if sidecar.get("row_kind") not in WAVE4_ROW_LABEL:
            raise OrbitError("Wave 4 physical row has an unknown logical role")
        if row.get("label") is not WAVE4_ROW_LABEL[str(sidecar["row_kind"])]:
            raise OrbitError("Wave 4 row label disagrees with its logical role")
        previous = rows_by_pair.get(pair_id)
        if previous is not None and previous != record:
            raise OrbitError(f"Wave 4 physical pair ID collision: {pair_id}")
        rows_by_pair.setdefault(pair_id, record)
        key = str(
            record.get("unordered_pair_key")
            or unordered_pair_key(
                render_hash(str(row["reference"])), render_hash(str(row["candidate"]))
            )
        )
        owner = unordered.get(key)
        if owner is not None and owner != (pair_id, bool(row["label"])):
            kind = "conflicting labels" if owner[1] != bool(row["label"]) else "duplicate pair"
            raise OrbitError(f"Wave 4 closure has {kind} across physical rows")
        unordered[key] = (pair_id, bool(row["label"]))

    groups_by_id: dict[str, Wave4ReleaseGroup] = {}
    expected_memberships: dict[str, set[str]] = {}
    per_root: dict[str, int] = {}
    for raw_group in group_records:
        record = dict(raw_group)
        group_id = _wave4_text(record.get("group_id"), "wave4.group.group_id")
        if group_id in groups_by_id:
            raise OrbitError(f"duplicate Wave 4 closure group {group_id}")
        root_id = _wave4_text(record.get("root_id"), "wave4.group.root_id")
        _wave4_text(record.get("operation_id"), "wave4.group.operation_id")
        negative_operation = _wave4_text(
            record.get("negative_operation"), "wave4.group.negative_operation"
        )
        if negative_operation == "N19_WHOLE_CLAIM_NEGATION_V1":
            raise OrbitError("N19 is forbidden from Wave 4 closure groups")
        _wave4_text(record.get("negative_mechanism"), "wave4.group.negative_mechanism")
        depth = _wave4_nat(record.get("depth"), "wave4.group.depth")
        if not 1 <= depth <= 3:
            raise OrbitError("Wave 4 logical group depth is outside one to three")
        for field_name in (
            "reference_operation_chain",
            "candidate_operation_chain",
            "preserving_mechanism_chain",
            "preserving_superclass_chain",
        ):
            chain = _wave4_sequence(record.get(field_name), f"wave4.group.{field_name}")
            if len(chain) != depth:
                raise OrbitError(f"Wave 4 logical group {field_name} has the wrong depth")
            for index, value in enumerate(chain):
                _wave4_text(value, f"wave4.group.{field_name}[{index}]")
        for field_name in (
            "selection_hash",
            "content_hash",
            "reference_chain_hash",
            "candidate_chain_hash",
            "reference_site_hash",
            "candidate_site_hash",
            "base_negative_evidence_hash",
            "negative_last_replay_hash",
            "closure_certificate_hash",
        ):
            _wave4_sha256(record.get(field_name), f"wave4.group.{field_name}")
        logical = _wave4_mapping(record.get("logical_pair_ids"), "wave4.group.logical_pair_ids")
        if set(logical) != set(EDGE_ROLES):
            raise OrbitError("Wave 4 logical closure group is partial or noncanonical")
        if len({str(value) for value in logical.values()}) != len(EDGE_ROLES):
            raise OrbitError("Wave 4 logical closure group repeats a physical row")
        for role in EDGE_ROLES:
            pair_id = _wave4_text(logical.get(role), f"wave4.group.logical_pair_ids.{role}")
            row_record = rows_by_pair.get(pair_id)
            if row_record is None:
                raise OrbitError("Wave 4 logical closure group references a missing physical row")
            sidecar = _wave4_mapping(row_record["sidecar"], "wave4.record.sidecar")
            if sidecar.get("row_kind") != role:
                raise OrbitError("Wave 4 logical role points to a differently typed physical row")
            if sidecar.get("root_id") != root_id:
                raise OrbitError("Wave 4 logical group crosses ancestry roots")
            if sidecar.get("operation_id") != record.get("operation_id"):
                raise OrbitError("Wave 4 logical group crosses orbit operations")
            if sidecar.get("negative_operation") != negative_operation:
                raise OrbitError("Wave 4 logical group changes negative families")
            evidence = _wave4_mapping(sidecar.get("evidence"), "wave4.record.evidence")
            if role in {"negative_base", "negative_last"} and hash_canonical(
                evidence.get("negative_family_evidence")
            ) != record.get("base_negative_evidence_hash"):
                raise OrbitError("Wave 4 row drops its negative-family evidence")
            if role == "negative_last" and hash_canonical(
                evidence.get("negative_last_replay")
            ) != record.get("negative_last_replay_hash"):
                raise OrbitError("Wave 4 terminal row drops its exact negative-last replay")
            expected_memberships.setdefault(pair_id, set()).add(group_id)
        # Canonical JSON deliberately sorts object keys, so mapping insertion order
        # cannot be part of the persisted closure contract.  Validate the exact role
        # set above, then restore the semantic edge order for every rehydrated group.
        canonical_record = dict(record)
        canonical_record["logical_pair_ids"] = {
            role: _wave4_text(logical.get(role), f"wave4.group.logical_pair_ids.{role}")
            for role in EDGE_ROLES
        }
        groups_by_id[group_id] = Wave4ReleaseGroup(canonical_record)
        per_root[root_id] = per_root.get(root_id, 0) + 1
        if per_root[root_id] > 5:
            raise OrbitError("Wave 4 closure exceeds five selected variants per root")

    if set(rows_by_pair) != set(expected_memberships):
        raise OrbitError("Wave 4 materialization contains orphan physical rows")
    for pair_id, record in rows_by_pair.items():
        sidecar = _wave4_mapping(record["sidecar"], "wave4.record.sidecar")
        actual = tuple(str(value) for value in sidecar.get("closure_group_ids") or [])
        expected = tuple(sorted(expected_memberships[pair_id]))
        if actual != expected:
            raise OrbitError("Wave 4 physical-row group memberships are incomplete")
        if len(expected) > 1 and sidecar.get("row_kind") != "negative_base":
            raise OrbitError("only the exact Wave 4 base negative may be physically shared")
        if record.get("row_hash") != _wave4_row_hash(
            cast(Mapping[str, Any], record["row"]), sidecar
        ):
            raise OrbitError("Wave 4 physical row hash does not bind its closure memberships")
    return Wave4ClosureMaterialization(
        rows=tuple(rows_by_pair[pair_id] for pair_id in sorted(rows_by_pair)),
        groups=tuple(groups_by_id[group_id] for group_id in sorted(groups_by_id)),
    )


def _rematerialize_wave4_selection(
    source: Wave4ClosureMaterialization, selected_groups: Sequence[Wave4ReleaseGroup]
) -> Wave4ClosureMaterialization:
    memberships: dict[str, set[str]] = {}
    selected_group_ids = {group.group_id for group in selected_groups}
    for group in selected_groups:
        for pair_id in group.row_ids:
            memberships.setdefault(pair_id, set()).add(group.group_id)
    rows: list[dict[str, Any]] = []
    for source_record in source.rows:
        source_sidecar = cast(Mapping[str, Any], source_record["sidecar"])
        pair_id = str(source_sidecar["pair_id"])
        if pair_id not in memberships:
            continue
        record = dict(source_record)
        sidecar = dict(source_sidecar)
        sidecar["closure_group_ids"] = sorted(memberships[pair_id])
        record["sidecar"] = sidecar
        record["row_hash"] = _wave4_row_hash(cast(Mapping[str, Any], record["row"]), sidecar)
        rows.append(record)
    groups = tuple(group for group in source.groups if group.group_id in selected_group_ids)
    return materialize_wave4_records(rows, [group.record for group in groups])


def _balance_wave4_pair_delta_units(
    materialized: Wave4ClosureMaterialization, *, selection_salt: str
) -> tuple[Wave4ClosureMaterialization, dict[str, object]]:
    """Balance pair-delta cells without breaking shared-edge closure groups.

    Rows cannot be sampled independently because a Wave 4 certificate is a four-edge
    logical group and several variants may share one physical negative edge.  The
    smallest additive selection unit is therefore the complete set of currently
    selected groups for one ancestry root.  Zero-delta units are retained directly;
    nonzero units are retained only in stable pairs whose complete cell-delta vectors
    cancel.  Unmatched units are quarantined as whole closure units, with the exact
    offending cells recorded, rather than invalidating unrelated cells or the wave.
    """

    from leanfaith.sft1.sprint.views import wave3_pair_delta

    rows_by_id = {
        str(cast(Mapping[str, Any], record["sidecar"])["pair_id"]): record
        for record in materialized.rows
    }
    groups_by_root: dict[str, list[Wave4ReleaseGroup]] = {}
    for group in materialized.groups:
        groups_by_root.setdefault(group.root_id, []).append(group)

    def cell_counts(group_set: Sequence[Wave4ReleaseGroup]) -> dict[str, dict[str, int]]:
        pair_ids = {pair_id for group in group_set for pair_id in group.row_ids}
        counts: dict[str, dict[str, int]] = {}
        for pair_id in sorted(pair_ids):
            record = rows_by_id[pair_id]
            cell = str(wave3_pair_delta(record)["cell"])
            polarity = (
                "positive" if bool(cast(Mapping[str, Any], record["row"])["label"]) else "negative"
            )
            counts.setdefault(cell, {"positive": 0, "negative": 0})[polarity] += 1
        return counts

    def vector(counts: Mapping[str, Mapping[str, int]]) -> tuple[tuple[str, int], ...]:
        return tuple(
            (cell, int(values.get("positive", 0)) - int(values.get("negative", 0)))
            for cell, values in sorted(counts.items())
            if int(values.get("positive", 0)) != int(values.get("negative", 0))
        )

    def project_ids(group_set: Sequence[Wave4ReleaseGroup]) -> tuple[str, ...]:
        projects: set[str] = set()
        for pair_id in {pair_id for group in group_set for pair_id in group.row_ids}:
            record = rows_by_id[pair_id]
            sidecar = cast(Mapping[str, Any], record["sidecar"])
            project = sidecar.get("project")
            source = record.get("_release_source")
            if isinstance(project, Mapping) and isinstance(project.get("project_id"), str):
                projects.add(str(project["project_id"]))
            elif isinstance(source, Mapping) and isinstance(source.get("project_id"), str):
                projects.add(str(source["project_id"]))
        return tuple(sorted(projects or {"unknown"}))

    units: list[dict[str, Any]] = []
    for root_id, root_groups in sorted(groups_by_root.items()):
        ordered_groups = tuple(sorted(root_groups, key=lambda group: group.group_id))
        counts = cell_counts(ordered_groups)
        units.append(
            {
                "root_id": root_id,
                "groups": ordered_groups,
                "vector": vector(counts),
                "cell_counts": counts,
                "projects": project_ids(ordered_groups),
                "negative_operations": tuple(
                    sorted({str(group.record["negative_operation"]) for group in ordered_groups})
                ),
                "preserving_superclasses": tuple(
                    sorted(
                        {
                            superclass
                            for group in ordered_groups
                            for superclass in cast(
                                Sequence[str], group.record["preserving_superclass_chain"]
                            )
                        }
                    )
                ),
            }
        )

    def stable_interleave(values: Sequence[dict[str, Any]], salt: str) -> list[dict[str, Any]]:
        buckets: dict[tuple[object, ...], list[dict[str, Any]]] = {}
        for unit in values:
            stratum = (
                unit["projects"],
                unit["negative_operations"],
                unit["preserving_superclasses"],
            )
            buckets.setdefault(stratum, []).append(unit)
        queues = [
            deque(
                sorted(
                    buckets[key],
                    key=lambda unit: hash_canonical([salt, key, unit["root_id"], unit["vector"]]),
                )
            )
            for key in sorted(buckets, key=lambda value: hash_canonical([salt, value]))
        ]
        ordered: list[dict[str, Any]] = []
        while queues:
            remaining = []
            for queue in queues:
                ordered.append(queue.popleft())
                if queue:
                    remaining.append(queue)
            queues = remaining
        return ordered

    by_vector: dict[tuple[tuple[str, int], ...], list[dict[str, Any]]] = {}
    for unit in units:
        by_vector.setdefault(cast(tuple[tuple[str, int], ...], unit["vector"]), []).append(unit)
    selected_units = stable_interleave(by_vector.pop((), []), f"{selection_salt}:pair-delta:zero")
    bucket_receipts: list[dict[str, object]] = []
    visited: set[tuple[tuple[str, int], ...]] = set()
    for key in sorted(by_vector, key=lambda value: hash_canonical(value)):
        if key in visited:
            continue
        inverse = tuple((cell, -delta) for cell, delta in key)
        left = stable_interleave(by_vector[key], f"{selection_salt}:pair-delta:left:{key}")
        right = stable_interleave(
            by_vector.get(inverse, []), f"{selection_salt}:pair-delta:right:{inverse}"
        )
        matched = min(len(left), len(right))
        selected_units.extend(left[:matched])
        selected_units.extend(right[:matched])
        bucket_receipts.append(
            {
                "vector": [[cell, delta] for cell, delta in key],
                "inverse": [[cell, delta] for cell, delta in inverse],
                "available": len(left),
                "inverse_available": len(right),
                "matched_each": matched,
            }
        )
        visited.add(key)
        visited.add(inverse)

    selected_root_ids = {str(unit["root_id"]) for unit in selected_units}
    selected_groups = [group for group in materialized.groups if group.root_id in selected_root_ids]
    balanced = _rematerialize_wave4_selection(materialized, selected_groups)
    before_counts = cell_counts(materialized.groups)
    after_counts = cell_counts(balanced.groups)
    if any(values["positive"] != values["negative"] for values in after_counts.values()):
        raise OrbitError("Wave 4 pair-delta unit matching failed to balance its retained cells")
    dropped_units = [unit for unit in units if str(unit["root_id"]) not in selected_root_ids]
    dropped_group_ids = sorted(
        group.group_id
        for unit in dropped_units
        for group in cast(Sequence[Wave4ReleaseGroup], unit["groups"])
    )
    quarantined_cells = sorted(
        {
            cell
            for unit in dropped_units
            for cell, _delta in cast(tuple[tuple[str, int], ...], unit["vector"])
        }
    )
    report: dict[str, object] = {
        "policy": "whole_ancestry_closure_inverse_pair_delta_match_v1",
        "salt": selection_salt,
        "input_roots": len(units),
        "selected_roots": len(selected_units),
        "input_groups": len(materialized.groups),
        "selected_groups": len(balanced.groups),
        "input_physical_rows": len(materialized.rows),
        "selected_physical_rows": len(balanced.rows),
        "cell_counts_before": before_counts,
        "cell_counts_after": after_counts,
        "inverse_vector_buckets": bucket_receipts,
        "quarantined_cells": quarantined_cells,
        "quarantined_root_ids": sorted(str(unit["root_id"]) for unit in dropped_units),
        "quarantined_group_ids": dropped_group_ids,
        "quarantine_reason": (
            "unmatched_complete_closure_unit_pair_delta_vector" if dropped_group_ids else None
        ),
        "distribution_interleave": [
            "project",
            "negative_operation",
            "preserving_superclass_chain",
        ],
        "passed": bool(balanced.rows)
        and all(values["positive"] == values["negative"] for values in after_counts.values()),
    }
    return balanced, report


def select_wave4_release_groups(
    materialized: Wave4ClosureMaterialization,
    *,
    maximum_rows: int | None,
    n25_maximum_share: float,
    selection_salt: str,
    enforce_pair_delta_balance: bool = False,
) -> Wave4ClosureSelection:
    """Apply stable whole-group capacity, N25, and optional pair-delta balancing."""

    if maximum_rows is not None and maximum_rows <= 0:
        raise OrbitError("Wave 4 maximum rows must be positive")
    rows_by_pair = {
        str(cast(Mapping[str, Any], record["sidecar"])["pair_id"]): record
        for record in materialized.rows
    }

    def group_stratum(group: Wave4ReleaseGroup) -> tuple[object, ...]:
        first = rows_by_pair[group.row_ids[0]]
        sidecar = cast(Mapping[str, Any], first["sidecar"])
        source = first.get("_release_source")
        project = sidecar.get("project")
        project_id = (
            str(project["project_id"])
            if isinstance(project, Mapping) and isinstance(project.get("project_id"), str)
            else str(source["project_id"])
            if isinstance(source, Mapping) and isinstance(source.get("project_id"), str)
            else "unknown"
        )
        return (
            project_id,
            str(group.record["negative_operation"]),
            tuple(cast(Sequence[str], group.record["preserving_superclass_chain"])),
        )

    def interleave_groups(
        groups: Sequence[Wave4ReleaseGroup], *, salt: str
    ) -> list[Wave4ReleaseGroup]:
        buckets: dict[tuple[object, ...], list[Wave4ReleaseGroup]] = {}
        for group in groups:
            buckets.setdefault(group_stratum(group), []).append(group)
        queues = [
            deque(
                sorted(
                    buckets[key],
                    key=lambda group: (
                        hash_canonical(
                            {
                                "kind": "sft1_wave4_release_group_rank_v2",
                                "salt": salt,
                                "stratum": key,
                                "group_id": group.group_id,
                                "row_ids": list(group.row_ids),
                            }
                        ),
                        group.group_id,
                    ),
                )
            )
            for key in sorted(buckets, key=lambda value: hash_canonical([salt, value]))
        ]
        result: list[Wave4ReleaseGroup] = []
        while queues:
            remaining = []
            for queue in queues:
                result.append(queue.popleft())
                if queue:
                    remaining.append(queue)
            queues = remaining
        return result

    non_n25 = [
        group
        for group in materialized.groups
        if group.record["negative_operation"] != "N25_TOGGLE_EQ_NE_PROOF_V1"
    ]
    n25 = [
        group
        for group in materialized.groups
        if group.record["negative_operation"] == "N25_TOGGLE_EQ_NE_PROOF_V1"
    ]
    ordered = interleave_groups(non_n25, salt=f"{selection_salt}:non-n25")
    ordered.extend(interleave_groups(n25, salt=f"{selection_salt}:n25"))
    selected: list[Wave4ReleaseGroup] = []
    selected_rows: set[str] = set()
    capacity_dropped: list[str] = []
    for group in ordered:
        candidate_rows = selected_rows.union(group.row_ids)
        if maximum_rows is not None and len(candidate_rows) > maximum_rows:
            capacity_dropped.append(group.group_id)
            continue
        selected.append(group)
        selected_rows = candidate_rows
    pre_joint_groups: tuple[Wave4ReleaseGroup, ...] = tuple(selected)
    current_groups = pre_joint_groups
    cumulative_share_drops: set[str] = set()
    cumulative_pair_delta_drops: set[str] = set()
    cumulative_pair_delta_roots: set[str] = set()
    cumulative_pair_delta_cells: set[str] = set()
    pair_delta_report: dict[str, object] = {
        "policy": "not_requested",
        "passed": None,
        "quarantined_group_ids": [],
    }
    joint_iterations = 0
    while True:
        joint_iterations += 1
        before_ids = {group.group_id for group in current_groups}
        share_result = cap_negative_operation_share(
            current_groups,
            "N25_TOGGLE_EQ_NE_PROOF_V1",
            n25_maximum_share,
            selection_salt=f"{selection_salt}:n25:iteration:{joint_iterations}",
        )
        cumulative_share_drops.update(share_result.report.dropped_group_ids)
        final = _rematerialize_wave4_selection(materialized, share_result.selected_groups)
        if enforce_pair_delta_balance:
            final, pair_delta_report = _balance_wave4_pair_delta_units(
                final, selection_salt=f"{selection_salt}:iteration:{joint_iterations}"
            )
            cumulative_pair_delta_drops.update(
                cast(Sequence[str], pair_delta_report["quarantined_group_ids"])
            )
            cumulative_pair_delta_roots.update(
                cast(Sequence[str], pair_delta_report["quarantined_root_ids"])
            )
            cumulative_pair_delta_cells.update(
                cast(Sequence[str], pair_delta_report["quarantined_cells"])
            )
        after_ids = {group.group_id for group in final.groups}
        current_groups = final.groups
        if after_ids == before_ids:
            break
        if joint_iterations > len(selected) + 1:
            raise OrbitError("Wave 4 joint N25/pair-delta selection did not converge")

    negative_share_report = share_result.report.record()
    input_row_ids = {pair_id for group in pre_joint_groups for pair_id in group.row_ids}
    selected_row_ids = {pair_id for group in final.groups for pair_id in group.row_ids}
    input_n25_groups = [
        group for group in pre_joint_groups if group.operation_id == "N25_TOGGLE_EQ_NE_PROOF_V1"
    ]
    selected_n25_groups = [
        group for group in final.groups if group.operation_id == "N25_TOGGLE_EQ_NE_PROOF_V1"
    ]
    input_n25_rows = {pair_id for group in input_n25_groups for pair_id in group.row_ids}
    selected_n25_rows = {pair_id for group in selected_n25_groups for pair_id in group.row_ids}
    negative_share_report.update(
        {
            "joint_selection_iterations": joint_iterations,
            "input_group_count": len(pre_joint_groups),
            "selected_group_count": len(final.groups),
            "dropped_group_count": len(pre_joint_groups) - len(final.groups),
            "operation_input_group_count": len(input_n25_groups),
            "operation_selected_group_count": len(selected_n25_groups),
            "operation_dropped_group_count": len(input_n25_groups) - len(selected_n25_groups),
            "input_row_count": len(input_row_ids),
            "selected_row_count": len(selected_row_ids),
            "dropped_row_count": len(input_row_ids.difference(selected_row_ids)),
            "operation_input_row_count": len(input_n25_rows),
            "operation_selected_row_count": len(selected_n25_rows),
            "operation_dropped_row_count": len(input_n25_rows.difference(selected_n25_rows)),
            "maximum_operation_row_count": int(len(selected_row_ids) * n25_maximum_share),
            "dropped_group_ids": sorted(cumulative_share_drops),
            "cumulative_dropped_group_ids": sorted(cumulative_share_drops),
        }
    )
    if enforce_pair_delta_balance:
        pair_delta_report = dict(pair_delta_report)
        pair_delta_report.update(
            {
                "joint_selection_iterations": joint_iterations,
                "quarantined_group_ids": sorted(cumulative_pair_delta_drops),
                "quarantined_root_ids": sorted(cumulative_pair_delta_roots),
                "quarantined_cells": sorted(cumulative_pair_delta_cells),
                "quarantine_reason": (
                    "unmatched_complete_closure_unit_pair_delta_vector"
                    if cumulative_pair_delta_drops
                    else None
                ),
            }
        )
    return Wave4ClosureSelection(
        materialized=final,
        input_group_count=len(materialized.groups),
        input_physical_rows=len(materialized.rows),
        capacity_dropped_group_ids=tuple(sorted(capacity_dropped)),
        negative_share_report=negative_share_report,
        pair_delta_balance_report=pair_delta_report,
    )


def attach_cache_records(
    records: Sequence[dict[str, Any]], cache_root: Path
) -> list[dict[str, Any]]:
    """Attach and verify live square records so a no-regenerate build can snapshot them."""
    attached: list[dict[str, Any]] = []
    for item in records:
        record = dict(item)
        block = cast(dict[str, Any], cast(dict[str, Any], record["sidecar"]).get("cache") or {})
        relative = str(block.get("path") or "")
        path = cache_root / relative
        if not relative or not path.is_file():
            raise SquareError(f"cache record absent for {record.get('root_name')}: {path}")
        cached = read_json_object(path)
        if hash_canonical(cached) != block.get("content_sha256"):
            raise SquareError(f"cache content hash mismatch for {record.get('root_name')}: {path}")
        record["cache_record"] = cached
        record["cache_record_path"] = str(path.resolve())
        record["cache_record_file_sha256"] = hash_file(path)
        attached.append(record)
    return attached


_REBUILD_CALL = re.compile(r"rebuildSquares #\[(.*?)\](?: \"[^\"]*\")?\n")
_STRING_LITERAL = re.compile(r'"(?:[^"\\]|\\.)*"')


def _square_rebuild_entries(raw: Mapping[str, Any]) -> dict[int, dict[str, str]] | None:
    decoder = json.JSONDecoder()
    response = raw.get("response") or {}
    for message in cast(list[dict[str, Any]], response.get("messages") or []):
        data = str(message.get("data", ""))
        if '"square_rebuild"' not in data:
            continue
        start = data.find("{")
        if start < 0:
            continue
        payload, _ = decoder.raw_decode(data[start:])
        if payload.get("kind") != "square_rebuild":
            continue
        return {
            int(entry["index"]): {ep: str(entry[ep]) for ep in ("p", "c", "p_prime", "c_prime")}
            for entry in payload.get("squares", [])
        }
    return None


RawIndex = dict[str, list[Path]]


def index_raw_files(raw_dir: Path) -> RawIndex:
    """Request hash -> stored response files, built once instead of globbing per root."""
    index: RawIndex = {}
    for path in raw_dir.iterdir():
        if path.suffix == ".json":
            index.setdefault(path.name.split(".", 1)[0], []).append(path)
    for paths in index.values():
        paths.sort()
    return index


def _raw_files(raw_dir: Path, request_hash: str, raw_index: RawIndex | None) -> list[Path]:
    if raw_index is not None:
        return list(raw_index.get(request_hash, []))
    return sorted(raw_dir.glob(f"{request_hash}.*.json"))


class _RenderResponseCache:
    """Parsed (chunk names, rebuild entries) per stored render response, bounded."""

    def __init__(self, capacity: int = 32) -> None:
        self.capacity = capacity
        self.items: dict[Path, tuple[list[str], dict[int, dict[str, str]] | None] | str] = {}

    def get(self, path: Path) -> tuple[list[str], dict[int, dict[str, str]] | None] | str:
        cached = self.items.get(path)
        if cached is not None:
            return cached
        raw = read_json_object(path)
        code = str(cast(dict[str, Any], raw.get("request") or {}).get("code") or "")
        call = _REBUILD_CALL.search(code)
        if call is None:
            result: tuple[list[str], dict[int, dict[str, str]] | None] | str = (
                "rebuild_call_not_found"
            )
        else:
            names = [json.loads(literal) for literal in _STRING_LITERAL.findall(call.group(1))]
            result = (names, _square_rebuild_entries(raw))
        if len(self.items) >= self.capacity:
            self.items.pop(next(iter(self.items)))
        self.items[path] = result
        return result


_RENDER_CACHE = _RenderResponseCache()


def reconcile_square_alpha(
    record: Mapping[str, Any], raw_dir: Path, raw_index: RawIndex | None = None
) -> dict[str, Any]:
    """Compare the persisted process alpha hashes with the structural hashes that
    ``rebuildSquares`` emitted in the stored render response for the same root.

    Fail-closed: any missing raw file, unparsable call, index or name disagreement, or
    hash difference on any of the four endpoints reports ``matches == False``.
    """
    render = cast(dict[str, Any], record.get("render") or {})
    request_hash = str(render.get("request_hash") or "")
    process = {ep: str(v) for ep, v in cast(dict[str, Any], record.get("alpha") or {}).items()}
    result: dict[str, Any] = {
        "render_request_hash": request_hash,
        "chunk_index": None,
        "process": process,
        "rebuild": None,
        "raw_files": 0,
        "matches": False,
        "reason": None,
    }
    endpoint_id = str(
        cast(dict[str, Any], render.get("p") or {}).get("record", {}).get("endpoint_id", "")
    )
    if not request_hash or "." not in endpoint_id:
        result["reason"] = "render_record_incomplete"
        return result
    chunk_index = int(endpoint_id.split(".", 1)[0])
    result["chunk_index"] = chunk_index
    files = _raw_files(raw_dir, request_hash, raw_index)
    result["raw_files"] = len(files)
    if not files:
        result["reason"] = "raw_render_response_missing"
        return result
    rebuilds: list[dict[str, str]] = []
    for path in files:
        parsed = _RENDER_CACHE.get(path)
        if isinstance(parsed, str):
            result["reason"] = parsed
            return result
        names, entries = parsed
        if chunk_index >= len(names) or names[chunk_index] != record.get("root"):
            result["reason"] = "chunk_name_mismatch"
            return result
        if entries is None or chunk_index not in entries:
            result["reason"] = "square_rebuild_report_missing"
            return result
        rebuilds.append(entries[chunk_index])
    if any(item != rebuilds[0] for item in rebuilds[1:]):
        result["reason"] = "raw_responses_disagree"
        return result
    result["rebuild"] = rebuilds[0]
    if set(process) != {"p", "c", "p_prime", "c_prime"}:
        result["reason"] = "process_alpha_incomplete"
        return result
    result["matches"] = process == rebuilds[0]
    if not result["matches"]:
        result["reason"] = "alpha_mismatch:" + ",".join(
            ep for ep in ("p", "c", "p_prime", "c_prime") if process.get(ep) != rebuilds[0].get(ep)
        )
    return result


def generating_run_commits(
    runs_root: Path, operation_id: str = SQUARE_OPERATION
) -> dict[str, tuple[str, str]]:
    """Root -> (run id, implementation commit) for roots a square run processed through Lean.

    Used when a cache record predates the ``implementation_commit`` field: the generating
    run's manifest is the only truthful source of that commit.
    """
    result: dict[str, tuple[str, str]] = {}
    for run_dir in sorted(runs_root.glob("*")):
        manifest_path = run_dir / "run.json"
        journal_path = run_dir / "journal.jsonl"
        if not manifest_path.is_file() or not journal_path.is_file():
            continue
        manifest = read_json_object(manifest_path)
        if manifest.get("operation_id") != operation_id:
            continue
        commit = manifest.get("implementation_commit")
        if not isinstance(commit, str) or not commit:
            continue
        with journal_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if '"square_terminal"' not in line:
                    continue
                record = json.loads(line)
                if record.get("kind") == "square_terminal" and record.get("source") == "lean":
                    result.setdefault(str(record["root"]), (run_dir.name, commit))
    return result


def run_evidence_hashes(paths: RunPaths) -> dict[str, dict[str, str]]:
    """Root -> the process/render request hashes the run's own retained rows were built from."""
    result: dict[str, dict[str, str]] = {}
    if not paths.retained.is_file():
        return result
    with paths.retained.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            sidecar = json.loads(line)["sidecar"]
            result.setdefault(
                str(sidecar["root_name"]),
                {k: str(v) for k, v in (sidecar.get("lean_request_hashes") or {}).items()},
            )
    return result


def recover_square_record(
    paths: RunPaths,
    name: str,
    *,
    raw_dir: Path,
    operation_id: str,
    raw_index: RawIndex | None = None,
) -> dict[str, Any] | None:
    """Rebuild a retained square record from durable run evidence, without Lean.

    The run's retained rows carry every endpoint REPR record and source material, the
    square evidence, module, level parameters, and the request hashes; the stored process
    response (content-addressed by that hash) carries the alpha hashes, goals, and the
    evidence again, which must agree. Returns ``None`` when any piece is missing or the
    stored copies disagree.
    """
    rows = [
        json.loads(line)
        for line in paths.retained.read_text(encoding="utf-8").splitlines()
        if line and name in line
    ]
    rows = [item for item in rows if item["sidecar"].get("root_name") == name]
    if len(rows) != 4:
        return None
    sidecar = rows[0]["sidecar"]
    hashes = sidecar.get("lean_request_hashes") or {}
    render: dict[str, Any] = {"request_hash": str(hashes.get("render", ""))}
    for item in rows:
        block = item["sidecar"]["repr"]
        for side in ("reference", "candidate"):
            record = block[side]
            endpoint = str(record.get("endpoint_id", "")).split(".", 1)[-1]
            if endpoint in {"p", "c", "p_prime", "c_prime"} and endpoint not in render:
                render[endpoint] = {
                    "record": record,
                    "source_material": block[f"{side}_source_material"],
                }
    if any(ep not in render for ep in ("p", "c", "p_prime", "c_prime")):
        return None
    evidence = (sidecar.get("evidence") or {}).get("square")
    if not isinstance(evidence, dict):
        return None
    payloads: list[dict[str, Any]] = []
    for path in _raw_files(raw_dir, str(hashes.get("process", "")), raw_index):
        raw = read_json_object(path)
        messages = cast(list[dict[str, Any]], (raw.get("response") or {}).get("messages") or [])
        for payload in parse_evidence_lines(messages):
            if payload.get("kind") == "square" and payload.get("root") == name:
                payloads.append(dict(payload))
    if not payloads:
        return None
    first = payloads[0]
    for other in payloads[1:]:
        if other.get("alpha") != first.get("alpha") or other.get("evidence") != first.get(
            "evidence"
        ):
            return None
    if first.get("status") != "retained" or first.get("evidence") != evidence:
        return None
    return {
        "schema_version": 1,
        "operation_id": str(first.get("operation_id") or operation_id),
        "root": name,
        "status": "retained",
        "reason": "",
        "direction": first.get("direction"),
        "module": first.get("module"),
        "level_params": first.get("level_params"),
        "alpha": first.get("alpha"),
        "goals": first.get("goals"),
        "evidence": evidence,
        "elapsed_ms": first.get("elapsed_ms"),
        "engine": sidecar.get("engine"),
        "implementation_commit": None,
        "implementation_commit_source": f"recovered_from_run_evidence:{paths.run_dir.name}",
        "process_request_hash": str(hashes.get("process", "")),
        "render": render,
        "recovered_from": {
            "run_id": paths.run_dir.name,
            "process_raw_copies": len(payloads),
        },
    }


def regenerate_records(
    runner: SquareRunner, *, raw_dir: Path, roots_by_name: Mapping[str, Mapping[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Rebuild retained rows from the journal's retained terminals and the cache records.

    Every root must have a retained cache record with a render, an alpha reconciliation
    against its stored render response, and regenerated pair ids equal to the ones its
    terminal promised; otherwise the root is quarantined (never silently dropped).
    """
    rows: list[dict[str, Any]] = []
    quarantined: list[dict[str, Any]] = []
    generating = generating_run_commits(runner.paths.run_dir.parent, runner.operation_id)
    evidence_hashes = run_evidence_hashes(runner.paths)
    raw_index = index_raw_files(raw_dir)
    runner.recovered_roots = []
    for name, promised in terminal_pair_ids(runner.paths.journal).items():
        record = runner.cache.get_root(runner.square_root_key(name))
        original = evidence_hashes.get(name)
        live_matches_run = (
            record is not None
            and original is not None
            and record.get("process_request_hash") == original.get("process")
            and (record.get("render") or {}).get("request_hash") == original.get("render")
        )
        if not live_matches_run:
            # the shared cache no longer holds the record these rows were built from (or
            # never did); rebuild it from the run's own durable evidence
            recovered = recover_square_record(
                runner.paths,
                name,
                raw_dir=raw_dir,
                operation_id=runner.operation_id,
                raw_index=raw_index,
            )
            if recovered is None:
                quarantined.append(
                    {
                        "root": name,
                        "reason": "cache_record_missing_or_overwritten_and_unrecoverable",
                    }
                )
                continue
            record = recovered
            runner.recovered_roots.append(name)
        assert record is not None
        if (
            record.get("status") != "retained"
            or not isinstance(record.get("render"), dict)
            or record.get("root") != name
        ):
            quarantined.append({"root": name, "reason": "cache_record_missing_or_not_retained"})
            continue
        if not record.get("implementation_commit"):
            resolved = generating.get(name)
            if resolved is None:
                quarantined.append({"root": name, "reason": "implementation_commit_unresolvable"})
                continue
            run_name, commit = resolved
            record = {
                **record,
                "implementation_commit": commit,
                "implementation_commit_source": f"generating_run_manifest:{run_name}",
            }
        reconciliation = reconcile_square_alpha(record, raw_dir, raw_index)
        if not reconciliation["matches"]:
            quarantined.append(
                {
                    "root": name,
                    "reason": f"alpha_unreconciled:{reconciliation['reason']}",
                    "reconciliation": reconciliation,
                }
            )
            continue
        root = roots_by_name.get(name, {"name": name})
        built = runner.build_rows(name, record, root, reconciliation=reconciliation)
        if [str(item["sidecar"]["pair_id"]) for item in built] != promised:
            quarantined.append({"root": name, "reason": "pair_ids_differ_from_terminal"})
            continue
        for item in built:
            item["cache_record"] = record  # snapshotted by the release build, not serialized
        rows.extend(built)
    return rows, quarantined


@dataclass(frozen=True)
class SquareSelection:
    """Outcome of square-level selection: squares are accepted whole or dropped whole."""

    kept: list[dict[str, Any]]
    accepted_roots: list[str]
    duplicate_squares: list[dict[str, str]]
    degenerate_roots: list[str]
    conflict_rows: int
    superseded_squares: list[dict[str, str]] = field(default_factory=list)
    capacity_squares: list[str] = field(default_factory=list)


def square_key(sidecar: Mapping[str, Any]) -> str:
    """One square = one root under one square operation."""
    return f"{sidecar['root_id']}|{sidecar.get('operation_id', SQUARE_OPERATION)}"


def collapse_exact_repeated_records(
    records: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], int, int]:
    """Collapse equivalent rows repeated through overlapping source runs.

    A pair ID is content-addressed. Seeing the exact same payload twice is expected when a
    resumed wider target contains rows already emitted by an earlier bounded run. The one
    allowed provenance-only difference is ``runner_source_sha256``: release/audit-only changes
    to this module can change that hash without changing the engine, cache record, certificate,
    serialized pair, or any other sidecar field. Every other difference remains an integrity
    error. Return total repeated records and the provenance-only subset separately.
    """
    kept: list[dict[str, Any]] = []
    by_pair: dict[str, dict[str, Any]] = {}
    repeated = 0
    provenance_only = 0
    for item in records:
        record = dict(item)
        pair_id = str(cast(Mapping[str, Any], record["sidecar"])["pair_id"])
        previous = by_pair.get(pair_id)
        if previous is None:
            by_pair[pair_id] = record
            kept.append(record)
            continue
        if record != previous:
            previous_sidecar = dict(cast(Mapping[str, Any], previous["sidecar"]))
            record_sidecar = dict(cast(Mapping[str, Any], record["sidecar"]))
            previous_sidecar.pop("runner_source_sha256", None)
            record_sidecar.pop("runner_source_sha256", None)
            if {**record, "sidecar": record_sidecar} != {
                **previous,
                "sidecar": previous_sidecar,
            }:
                raise SquareError(f"pair ID collision across source runs: {pair_id}")
            provenance_only += 1
        repeated += 1
    return kept, repeated, provenance_only


def select_squares(
    screened: Sequence[Mapping[str, Any]],
    conflict_keys: Collection[str],
    preferred_operations: Collection[str] = (),
) -> SquareSelection:
    """Accept squares in the stable salted-hash order of their root id.

    A square whose rendered unordered pair keys were already claimed by an earlier square
    (Mathlib aliases and textually identical statements) is a duplicate and is dropped
    whole, with its owner recorded. Squares touching a conflicting key, lacking exactly the
    four row kinds, or colliding internally are degenerate and dropped fail-closed. Rows are
    never dropped individually, so every accepted root keeps exactly four rows.
    """
    kind_order = {kind: index for index, (kind, *_rest) in enumerate(ROW_KINDS)}
    by_root: dict[str, list[dict[str, Any]]] = {}
    for record in screened:
        by_root.setdefault(square_key(record["sidecar"]), []).append(dict(record))
    # one square per root when a preferred operation (the complete v4 square) is present
    preferred = set(preferred_operations)
    superseded: list[dict[str, str]] = []
    if preferred:
        squares_by_root: dict[str, list[str]] = {}
        for key in by_root:
            squares_by_root.setdefault(key.split("|", 1)[0], []).append(key)
        for root_id, keys in squares_by_root.items():
            winners = [k for k in keys if k.split("|", 1)[1] in preferred]
            if winners and len(keys) > 1:
                for key in keys:
                    if key not in winners:
                        superseded.append(
                            {
                                "square": key,
                                "root_id": root_id,
                                "operation_id": key.split("|", 1)[1],
                                "superseded_by": winners[0],
                            }
                        )
                        by_root.pop(key)
    salted = lambda key: hash_canonical([SQUARE_SALT, key])  # noqa: E731
    ordered = sorted((k for k in by_root if k.split("|", 1)[1] in preferred), key=salted) + sorted(
        (k for k in by_root if k.split("|", 1)[1] not in preferred), key=salted
    )
    conflicts = set(conflict_keys)
    claimed: dict[str, str] = {}
    kept: list[dict[str, Any]] = []
    accepted: list[str] = []
    duplicates: list[dict[str, str]] = []
    degenerate: list[str] = []
    conflict_rows = 0
    for root in ordered:
        items = sorted(
            by_root[root],
            key=lambda item: kind_order.get(str(item["sidecar"]["row_kind"]), len(kind_order)),
        )
        kinds = [str(item["sidecar"]["row_kind"]) for item in items]
        keys = [str(item["unordered_pair_key"]) for item in items]
        touching = sum(1 for key in keys if key in conflicts)
        conflict_rows += touching
        if touching or kinds != list(kind_order) or len(set(keys)) != len(keys):
            degenerate.append(root)
            continue
        owner = next((claimed[key] for key in keys if key in claimed), None)
        if owner is not None:
            duplicates.append(
                {
                    "square": root,
                    "root_id": root.split("|", 1)[0],
                    "operation_id": root.split("|", 1)[1],
                    "duplicate_of": owner,
                    "duplicate_of_root_id": owner.split("|", 1)[0],
                }
            )
            continue
        for key in keys:
            claimed[key] = root
        accepted.append(root)
        kept.extend(items)
    return SquareSelection(kept, accepted, duplicates, degenerate, conflict_rows, superseded)


def cap_square_selection(selection: SquareSelection, maximum_rows: int | None) -> SquareSelection:
    """Apply a stable whole-square row ceiling after structural selection."""
    if maximum_rows is None:
        return selection
    if maximum_rows <= 0 or maximum_rows % len(ROW_KINDS) != 0:
        raise SquareError(f"maximum rows must be a positive multiple of {len(ROW_KINDS)}")
    maximum_squares = maximum_rows // len(ROW_KINDS)
    accepted = selection.accepted_roots[:maximum_squares]
    accepted_set = set(accepted)
    capped = selection.accepted_roots[maximum_squares:]
    return SquareSelection(
        kept=[item for item in selection.kept if square_key(item["sidecar"]) in accepted_set],
        accepted_roots=accepted,
        duplicate_squares=selection.duplicate_squares,
        degenerate_roots=selection.degenerate_roots,
        conflict_rows=selection.conflict_rows,
        superseded_squares=selection.superseded_squares,
        capacity_squares=capped,
    )


def sidecar_aggregates(sidecars: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Every aggregate a manifest reports, derived from the finalized sidecars only."""
    counts: dict[str, dict[str, int]] = {
        "operations": {},
        "mechanisms": {},
        "negative_mechanisms": {},
        "transforms": {},
        "families": {},
        "row_kinds": {},
    }
    squares: set[str] = set()
    for sidecar in sidecars:
        square = cast(Mapping[str, Any], sidecar.get("square") or {})
        for name, value in (
            ("operations", sidecar.get("operation_id")),
            ("mechanisms", sidecar.get("mechanism")),
            ("negative_mechanisms", square.get("negative_operation")),
            ("transforms", square.get("t_p")),
            ("families", sidecar.get("core_family")),
            ("row_kinds", sidecar.get("row_kind")),
        ):
            key = str(value)
            counts[name][key] = counts[name].get(key, 0) + 1
        squares.add(square_key(sidecar))
    return {
        **{name: dict(sorted(values.items())) for name, values in counts.items()},
        "squares": len(squares),
        "curriculum_only": any(
            str(sidecar.get("operation_id")) == "SQUARE_N19_CURRICULUM_V1" for sidecar in sidecars
        ),
    }


def write_cache_snapshots(
    out: Path, shards: Sequence[Sequence[dict[str, Any]]]
) -> list[dict[str, Any]]:
    """Pack the cache record behind each square into ``cache_records/shard-XXXX.jsonl``.

    One canonical JSON line per square (in shard order); every row's sidecar cache block
    receives the file, line, and content hash of its record. Rows regenerated without a
    record (legacy retained files) keep ``snapshot: None``.
    """
    directory = out / "cache_records"
    directory.mkdir(parents=True, exist_ok=True)
    manifests: list[dict[str, Any]] = []
    for number, shard in enumerate(shards, start=1):
        name = f"cache_records/shard-{number:04d}.jsonl"
        lines: list[bytes] = []
        index: dict[str, int] = {}
        for item in shard:
            record = item.get("cache_record")
            if record is None:
                continue
            block = item["sidecar"]["cache"]
            sha = str(block.get("content_sha256"))
            if sha not in index:
                index[sha] = len(lines)
                lines.append(canonical_json_bytes(record))
            block["snapshot"] = {"file": name, "line": index[sha], "content_sha256": sha}
        payload = b"".join(line + b"\n" for line in lines)
        write_atomic(out / name, payload)
        manifests.append(
            {
                "file": name,
                "records": len(lines),
                "squares": len(lines),
                "sha256": sha256_hex(payload),
                "content_set_sha256": hash_canonical(sorted(index)),
            }
        )
    return manifests


CURRICULUM_SAMPLING_CONFIGS: tuple[dict[str, Any], ...] = (
    {"name": "n19_0pct", "weight": 0.0, "role": "excluded"},
    {"name": "n19_2pct", "weight": 0.02, "role": "initial_default"},
    {"name": "n19_5pct", "weight": 0.05, "role": "option"},
    {"name": "n19_10pct", "weight": 0.10, "role": "hard_ceiling"},
)


def curriculum_sampling_configs() -> dict[str, Any]:
    """Explicit mixing weights for the curriculum-only auxiliary set (never concatenated)."""
    return {
        "unit": "fraction_of_mixed_training_rows_drawn_from_this_view",
        "initial_default": "n19_2pct",
        "hard_ceiling": "n19_10pct",
        "configs": [dict(item) for item in CURRICULUM_SAMPLING_CONFIGS],
        "note": "keep separate from the headline core; the outer-negation-XOR shortcut is ~0.98",
    }


def _wave4_group_aggregates(groups: Sequence[Wave4ReleaseGroup]) -> dict[str, Any]:
    counts: dict[str, dict[str, int]] = {
        "negative_families": {},
        "preserving_families": {},
        "chain_depths": {},
        "orbit_operations": {},
    }
    roots: set[str] = set()
    for group in groups:
        roots.add(group.root_id)
        record = group.record
        values = (
            ("negative_families", group.operation_id),
            ("chain_depths", str(record.get("depth"))),
            ("orbit_operations", str(record.get("operation_id"))),
        )
        for category, value in values:
            counts[category][value] = counts[category].get(value, 0) + 1
        for mechanism in record.get("preserving_mechanism_chain") or []:
            key = str(mechanism)
            counts["preserving_families"][key] = counts["preserving_families"].get(key, 0) + 1
    return {
        **{name: dict(sorted(values.items())) for name, values in counts.items()},
        "ancestry_roots": len(roots),
    }


def _wave4_release_shards(
    materialized: Wave4ClosureMaterialization, shard_size: int, selection_salt: str
) -> list[tuple[list[dict[str, Any]], list[Wave4ReleaseGroup]]]:
    """Pack complete ancestry roots, their rows, and group indices into shards."""

    if shard_size <= 0:
        raise OrbitError("Wave 4 shard size must be positive")
    rows_by_root: dict[str, list[dict[str, Any]]] = {}
    groups_by_root: dict[str, list[Wave4ReleaseGroup]] = {}
    for row in materialized.rows:
        sidecar = cast(Mapping[str, Any], row["sidecar"])
        rows_by_root.setdefault(str(sidecar["root_id"]), []).append(row)
    for group in materialized.groups:
        groups_by_root.setdefault(group.root_id, []).append(group)
    if set(rows_by_root) != set(groups_by_root):
        raise OrbitError("Wave 4 rows and logical groups cover different ancestry roots")
    root_order = sorted(
        rows_by_root,
        key=lambda root_id: (hash_canonical([selection_salt, root_id]), root_id),
    )
    shards: list[tuple[list[dict[str, Any]], list[Wave4ReleaseGroup]]] = []
    current_rows: list[dict[str, Any]] = []
    current_groups: list[Wave4ReleaseGroup] = []
    for root_id in root_order:
        root_rows = sorted(
            rows_by_root[root_id],
            key=lambda item: str(cast(Mapping[str, Any], item["sidecar"])["pair_id"]),
        )
        root_groups = sorted(groups_by_root[root_id], key=lambda group: group.group_id)
        if current_rows and len(current_rows) + len(root_rows) > shard_size:
            shards.append((current_rows, current_groups))
            current_rows = []
            current_groups = []
        current_rows.extend(root_rows)
        current_groups.extend(root_groups)
    if current_rows:
        shards.append((current_rows, current_groups))
    return shards


def _wave4_release_builder_identity(repo_root: Path) -> dict[str, Any]:
    return {
        "commit": _git(repo_root, "rev-parse", "HEAD"),
        "dirty": bool(_git(repo_root, "status", "--porcelain")),
        "square_source_sha256": hash_file(Path(__file__)),
    }


def _wave4_inspection_receipts(
    paths: Sequence[Path], *, released_pair_ids: frozenset[str]
) -> dict[str, Any]:
    receipts: list[dict[str, Any]] = []
    inspected_pair_ids: set[str] = set()
    repeated_pair_ids: set[str] = set()
    for raw_path in paths:
        path = raw_path.resolve()
        document = read_json_object(path)
        sample_value = document.get("sample_path")
        sample = Path(str(sample_value)) if isinstance(sample_value, str) else None
        sample_hash = document.get("sample_sha256")
        sample_verified = (
            sample is not None
            and sample.is_file()
            and isinstance(sample_hash, str)
            and hash_file(sample) == sample_hash
        )
        pair_ids: list[str] = []
        if sample_verified and sample is not None:
            for item in read_retained(sample):
                row = item.get("row") or {}
                sidecar = item.get("sidecar") or {}
                value = row.get("pair_id") or sidecar.get("pair_id") or item.get("pair_id")
                if isinstance(value, str):
                    pair_ids.append(value)
        passed = (
            document.get("wrong_labels_found") == 0
            and document.get("rows_read_by_hand") == len(pair_ids)
            and bool(pair_ids)
            and len(pair_ids) == len(set(pair_ids))
            and set(pair_ids).issubset(released_pair_ids)
            and sample_verified
        )
        repeated_pair_ids.update(inspected_pair_ids.intersection(pair_ids))
        inspected_pair_ids.update(pair_ids)
        receipts.append(
            {
                "path": str(path),
                "sha256": hash_file(path),
                "sample_path": sample_value,
                "sample_sha256": sample_hash,
                "sample_hash_verified": sample_verified,
                "sample_pair_ids_sha256": hash_canonical(pair_ids),
                "sample_pairs": len(pair_ids),
                "rows_read_by_hand": document.get("rows_read_by_hand"),
                "wrong_labels_found": document.get("wrong_labels_found"),
                "passed": passed,
            }
        )
    return {
        "provided": bool(receipts),
        "receipts": receipts,
        "inspected_pair_ids_sha256": hash_canonical(sorted(inspected_pair_ids)),
        "released_pair_ids_sha256": hash_canonical(sorted(released_pair_ids)),
        "missing_pair_count": len(released_pair_ids.difference(inspected_pair_ids)),
        "unexpected_pair_count": len(inspected_pair_ids.difference(released_pair_ids)),
        "repeated_pair_count": len(repeated_pair_ids),
        "exact_release_coverage": inspected_pair_ids == set(released_pair_ids)
        and not repeated_pair_ids,
        "passed": bool(receipts)
        and all(receipt["passed"] is True for receipt in receipts)
        and inspected_pair_ids == set(released_pair_ids)
        and not repeated_pair_ids,
    }


def _load_wave4_composition_gate(path: Path, *, policy_hash: str) -> dict[str, Any]:
    resolved = path.resolve()
    document = read_json_object(resolved)
    checks = document.get("checks")
    if (
        document.get("schema_version") != WAVE4_COMPOSITION_GATE_SCHEMA
        or document.get("kind") != "sft1_wave4_composition_gate_v1"
        or document.get("policy_hash") != policy_hash
        or document.get("unique_ancestry_roots") != 200
        or document.get("passed") is not True
        or not isinstance(checks, Mapping)
        or not checks
        or not all(value is True for value in checks.values())
    ):
        raise SquareError("Wave 4 full release requires a passed, policy-matched 200-root gate")
    return {
        "source_path": str(resolved),
        "source_sha256": hash_file(resolved),
        "gate_id": document.get("gate_id"),
        "policy_hash": policy_hash,
        "unique_ancestry_roots": 200,
        "source_receipts_sha256": document.get("source_receipts_sha256"),
        "integrity_report_sha256": document.get("integrity_report_sha256"),
        "passed": True,
        "document": document,
    }


def _wave4_closure_edge_issues(materialized: Wave4ClosureMaterialization) -> list[str]:
    """Verify the four physical endpoints and group certificate hashes close exactly."""

    rows = {
        str(cast(Mapping[str, Any], item["sidecar"])["pair_id"]): item for item in materialized.rows
    }
    issues: list[str] = []
    for group in materialized.groups:
        logical = cast(Mapping[str, str], group.record["logical_pair_ids"])
        sidecars = {
            role: cast(Mapping[str, Any], rows[str(logical[role])]["sidecar"])
            for role in EDGE_ROLES
        }

        def endpoint(
            role: str,
            side: str,
            rows_by_role: Mapping[str, Mapping[str, Any]] = cast(
                Mapping[str, Mapping[str, Any]], sidecars
            ),
        ) -> object:
            record = cast(Mapping[str, Any], rows_by_role[role]["repr"])[side]
            return cast(Mapping[str, Any], record).get("provenance", {}).get("expr_hash")

        equalities = (
            (endpoint("preserving_reference", "reference"), endpoint("negative_last", "reference")),
            (endpoint("preserving_reference", "candidate"), endpoint("negative_base", "candidate")),
            (endpoint("preserving_candidate", "reference"), endpoint("negative_base", "reference")),
            (endpoint("preserving_candidate", "candidate"), endpoint("negative_last", "candidate")),
        )
        if any(left != right for left, right in equalities):
            issues.append(f"group {group.group_id} physical endpoints do not close")
        terminal_evidence = cast(Mapping[str, Any], sidecars["negative_last"]["evidence"])
        closure = terminal_evidence.get("closure") or {}
        if hash_canonical(closure.get("closure")) != group.record.get("closure_certificate_hash"):
            issues.append(f"group {group.group_id} closure certificate hash differs")
        hops = closure.get("hops") or []
        if isinstance(hops, Sequence) and not isinstance(hops, str | bytes):
            expected = {
                "reference_operation_chain": [hop.get("p_operation") for hop in hops],
                "candidate_operation_chain": [hop.get("c_operation") for hop in hops],
                "preserving_mechanism_chain": [hop.get("mechanism") for hop in hops],
                "preserving_superclass_chain": [hop.get("superclass") for hop in hops],
            }
            for field, value in expected.items():
                if group.record.get(field) != value:
                    issues.append(f"group {group.group_id} {field} differs from its evidence")
    return issues


def build_wave4_release(
    repo_root: Path,
    loaded: LoadedWave4Config,
    *,
    run_dirs: Sequence[Path],
    output_dir: Path,
    label: str = "wave4/composed_core_v1",
    maximum_rows: int | None = None,
    gate_200: bool = False,
    inspection_verdict_paths: Sequence[Path] = (),
    composition_gate_report: Path | None = None,
) -> dict[str, Any]:
    """Build an immutable multi-project Wave 4 release from explicit completed runs."""

    from leanfaith.sft1.sprint import shortcut

    if not run_dirs:
        raise SquareError("explicit Wave 4 release needs at least one --run-dir")
    resolved_runs = [path.resolve() for path in run_dirs]
    if len(resolved_runs) != len(set(resolved_runs)):
        raise SquareError("explicit Wave 4 run directories must be distinct")
    out = output_dir.resolve()
    if out.exists():
        raise SquareError(f"{out} already exists; Wave 4 releases are additive and immutable")
    builder = _wave4_release_builder_identity(repo_root.resolve())
    if builder["dirty"] is not False:
        raise SquareError("publishable Wave 4 release requires a clean release-builder worktree")
    from leanfaith.sft1.sprint.integrity import git_commit_is_ancestor

    if not git_commit_is_ancestor(repo_root, builder.get("commit")):
        raise SquareError("Wave 4 release-builder commit is not an ancestor of the release")
    if gate_200 and composition_gate_report is not None:
        raise SquareError("a 200-root gate cannot consume another composition gate")
    if not gate_200 and composition_gate_report is None:
        raise SquareError("full Wave 4 release requires --composition-gate-report")
    gate_source = (
        None
        if composition_gate_report is None
        else _load_wave4_composition_gate(
            composition_gate_report, policy_hash=loaded.policy.policy_hash
        )
    )

    receipts: list[dict[str, Any]] = []
    rows_by_pair: dict[str, dict[str, Any]] = {}
    groups_by_id: dict[str, dict[str, Any]] = {}
    for run_dir in sorted(resolved_runs, key=str):
        receipt, bundle = load_completed_wave4_run(
            repo_root, run_dir, policy_hash=loaded.policy.policy_hash
        )
        receipts.append(receipt)
        cache_root_value = receipt.get("cache_root")
        if not isinstance(cache_root_value, str) or not cache_root_value:
            raise SquareError(f"Wave 4 run {run_dir} lacks an exact cache root")
        source_cache_root = Path(cache_root_value)
        for source_item in bundle.rows:
            item = json.loads(json.dumps(source_item))
            item["_source_record_sha256"] = hash_canonical(source_item)
            item["_release_source"] = {
                "source_key": receipt["source_key"],
                "run_id": receipt["run_id"],
                "project_id": receipt["project_id"],
                "project_revision": receipt["project_revision"],
            }
            attached = attach_cache_records([item], source_cache_root)[0]
            pair_id = str(cast(Mapping[str, Any], attached["sidecar"])["pair_id"])
            previous = rows_by_pair.get(pair_id)
            if previous is not None:
                if previous["_source_record_sha256"] != attached["_source_record_sha256"]:
                    raise SquareError(f"Wave 4 pair {pair_id} conflicts across source runs")
                previous_source = cast(Mapping[str, Any], previous["_release_source"])
                if str(receipt["source_key"]) < str(previous_source["source_key"]):
                    rows_by_pair[pair_id] = attached
            else:
                rows_by_pair[pair_id] = attached
        for group in bundle.groups:
            previous_group = groups_by_id.get(group.group_id)
            if previous_group is not None and previous_group != group.record:
                raise SquareError(f"Wave 4 group {group.group_id} conflicts across source runs")
            groups_by_id.setdefault(group.group_id, group.record)

    if len({str(receipt["source_key"]) for receipt in receipts}) != len(receipts):
        raise SquareError("Wave 4 source receipts have duplicate content identities")
    projects = {str(receipt["project_id"]) for receipt in receipts}
    if projects != WAVE4_PROJECTS:
        raise SquareError(
            "Wave 4 release requires Mathlib, Physlib, and CSLib source runs; "
            f"observed {sorted(projects)}"
        )
    materialized = materialize_wave4_records(
        list(rows_by_pair.values()), list(groups_by_id.values())
    )

    runtime = loaded.runtime
    config = runtime.config
    gold = GoldBlocklist.load(
        repo_root / config.screens.gold_blocklist_path,
        expected_sha256=config.screens.gold_blocklist_sha256,
    )
    rejected_pairs: dict[str, str] = {}
    for record in materialized.rows:
        row = cast(Mapping[str, Any], record["row"])
        reason = (
            residue_violation(str(row["reference"]))
            or residue_violation(str(row["candidate"]))
            or ("self_pair_text" if row["reference"] == row["candidate"] else None)
            or (
                "gold_blocklist"
                if gold.hit(str(row["reference"])) or gold.hit(str(row["candidate"]))
                else None
            )
        )
        if reason:
            rejected_pairs[str(cast(Mapping[str, Any], record["sidecar"])["pair_id"])] = reason
    screened_groups = [
        group
        for group in materialized.groups
        if not set(group.row_ids).intersection(rejected_pairs)
    ]
    screened = _rematerialize_wave4_selection(materialized, screened_groups)
    negative_families = _wave4_mapping(
        loaded.raw.get("negative_families"), "wave4.config.negative_families"
    )
    shares = _wave4_mapping(
        negative_families.get("maximum_released_share"),
        "wave4.config.negative_families.maximum_released_share",
    )
    n25_share = shares.get("N25_TOGGLE_EQ_NE_PROOF_V1")
    if isinstance(n25_share, bool) or not isinstance(n25_share, int | float):
        raise SquareError("Wave 4 N25 maximum released share must be numeric")
    selection = select_wave4_release_groups(
        screened,
        maximum_rows=maximum_rows,
        n25_maximum_share=float(n25_share),
        selection_salt=loaded.policy.selection_salt,
        enforce_pair_delta_balance=True,
    )
    if not selection.materialized.rows:
        raise SquareError("Wave 4 release retained no complete certificate groups")

    receipt_documents = sorted(receipts, key=lambda item: str(item["source_key"]))
    release_id = WAVE4_RELEASE_ID_PREFIX + hash_canonical(
        {
            "schema_version": WAVE4_RELEASE_SCHEMA,
            "label": label,
            "policy_hash": loaded.policy.policy_hash,
            "source_receipts_sha256": hash_canonical(receipt_documents),
            "maximum_rows": maximum_rows,
            "gate_200": gate_200,
            "composition_gate_sha256": None
            if gate_source is None
            else gate_source["source_sha256"],
        }
    )
    release_rows: list[dict[str, Any]] = []
    for raw_record in selection.materialized.rows:
        record = json.loads(json.dumps(raw_record))
        row = cast(Mapping[str, Any], record["row"])
        sidecar = cast(dict[str, Any], record["sidecar"])
        source = cast(Mapping[str, Any], record["_release_source"])
        sidecar["release"] = {
            "schema_version": WAVE4_RELEASE_SCHEMA,
            "release_id": release_id,
            "source": source,
            "source_record_sha256": record["_source_record_sha256"],
            "source_cache_file": {
                "path": record["cache_record_path"],
                "file_sha256": record["cache_record_file_sha256"],
                "content_sha256": cast(Mapping[str, Any], sidecar["cache"])["content_sha256"],
            },
            "release_row_hash": _wave4_row_hash(row, sidecar),
        }
        record["sidecar"] = sidecar
        release_rows.append(record)
    kept = materialize_wave4_records(
        release_rows, [group.record for group in selection.materialized.groups]
    )
    closure_issues = _wave4_closure_edge_issues(kept)
    if closure_issues:
        raise SquareError("Wave 4 closure defect: " + "; ".join(closure_issues[:20]))
    released_projects = _count_by(
        [cast(Mapping[str, Any], item["_release_source"]) for item in kept.rows],
        "project_id",
    )
    if set(released_projects) != WAVE4_PROJECTS:
        raise SquareError("Wave 4 selected rows do not cover all three pinned projects")
    ancestry_roots = len({group.root_id for group in kept.groups})
    if gate_200 and ancestry_roots != 200:
        raise SquareError(
            f"Wave 4 composition gate requires exactly 200 ancestry roots, got {ancestry_roots}"
        )

    shards = _wave4_release_shards(kept, config.output.shard_size, loaded.policy.selection_salt)
    out.mkdir(parents=True)
    if gate_source is not None:
        gate_bytes = Path(str(gate_source["source_path"])).read_bytes()
        write_atomic(out / "composition_gate_report.json", gate_bytes)
        gate_source = {
            key: value
            for key, value in gate_source.items()
            if key not in {"document", "source_path"}
        }
        gate_source["file"] = "composition_gate_report.json"
        gate_source["sha256"] = sha256_hex(gate_bytes)
    rejection_records = [
        {"pair_id": pair_id, "reason": reason} for pair_id, reason in sorted(rejected_pairs.items())
    ]
    rejection_bytes = canonical_json_bytes(rejection_records) + b"\n"
    write_atomic(out / "screen_rejections.json", rejection_bytes)
    capacity_bytes = canonical_json_bytes(list(selection.capacity_dropped_group_ids)) + b"\n"
    write_atomic(out / "capacity_dropped_groups.json", capacity_bytes)
    negative_share = dict(selection.negative_share_report)
    share_dropped = cast(list[str], negative_share.pop("dropped_group_ids"))
    share_bytes = canonical_json_bytes(share_dropped) + b"\n"
    write_atomic(out / "negative_share_dropped_groups.json", share_bytes)
    negative_share.update(
        {
            "dropped_group_ids_count": len(share_dropped),
            "dropped_group_ids_sha256": sha256_hex(share_bytes),
            "dropped_group_ids_file": "negative_share_dropped_groups.json",
        }
    )
    pair_delta_balance = dict(selection.pair_delta_balance_report)
    pair_delta_dropped = cast(list[str], pair_delta_balance.pop("quarantined_group_ids"))
    pair_delta_drop_bytes = canonical_json_bytes(pair_delta_dropped) + b"\n"
    write_atomic(out / "pair_delta_quarantined_groups.json", pair_delta_drop_bytes)
    pair_delta_balance.update(
        {
            "quarantined_group_ids_count": len(pair_delta_dropped),
            "quarantined_group_ids_sha256": sha256_hex(pair_delta_drop_bytes),
            "quarantined_group_ids_file": "pair_delta_quarantined_groups.json",
        }
    )
    snapshots = write_cache_snapshots(out, [rows for rows, _groups in shards])
    provenance = derive_provenance(
        kept.rows,
        repo_root=repo_root,
        cache_root=out / "no_live_cache",
        release_dir=out,
        allow_multiple_project_pins=True,
    )
    if not provenance["consistent"]:
        raise SquareError("Wave 4 provenance inconsistent: " + "; ".join(provenance["issues"]))

    shard_manifests: list[dict[str, Any]] = []
    for number, (rows, groups) in enumerate(shards, start=1):
        shard_dir = out / f"shard-{number:04d}"
        shard_dir.mkdir()
        rows_bytes = b"".join(canonical_json_bytes(item["row"]) + b"\n" for item in rows)
        sidecars_bytes = b"".join(canonical_json_bytes(item["sidecar"]) + b"\n" for item in rows)
        groups_bytes = b"".join(canonical_json_bytes(group.record) + b"\n" for group in groups)
        write_atomic(shard_dir / "rows.jsonl", rows_bytes)
        write_atomic(shard_dir / "sidecars.jsonl", sidecars_bytes)
        write_atomic(shard_dir / "closure_groups.jsonl", groups_bytes)
        shard_manifest = {
            "schema_version": WAVE4_RELEASE_SCHEMA,
            "row_schema": ROW_SCHEMA,
            "closure_schema": "sft1_wave4_shared_edge_closure_v1",
            "release_id": release_id,
            "view": label,
            "shard": number,
            "row_count": len(rows),
            "logical_group_count": len(groups),
            "logical_row_count": len(groups) * len(EDGE_ROLES),
            "roots": len({group.root_id for group in groups}),
            "rows_sha256": sha256_hex(rows_bytes),
            "sidecars_sha256": sha256_hex(sidecars_bytes),
            "closure_groups_sha256": sha256_hex(groups_bytes),
            "complete": True,
            "finalized": True,
        }
        shard_manifest["content_sha256"] = hash_canonical(shard_manifest)
        write_atomic(shard_dir / "manifest.json", canonical_json_bytes(shard_manifest) + b"\n")
        shard_manifests.append(shard_manifest)

    diagnostic_rows = [{"row": item["row"]} for item in kept.rows]
    pair_delta = shortcut.pairwise_shortcut_diagnostics(diagnostic_rows)
    xor_baseline = shortcut.outer_negation_xor_baseline(diagnostic_rows)
    sample, sample_receipt = shortcut.screen_sample(out)
    screens = shortcut.run_screens_v3(sample)
    control = shortcut.permutation_control(sample)
    diagnostics = {
        "pairwise": pair_delta,
        "outer_negation_xor": xor_baseline,
        "permutation_control": control,
    }
    diagnostic_bytes = canonical_json_bytes(diagnostics) + b"\n"
    screen_bytes = canonical_json_bytes(screens) + b"\n"
    write_atomic(out / "pairwise_diagnostics.json", diagnostic_bytes)
    write_atomic(out / "shortcut_screens.json", screen_bytes)
    inspections = _wave4_inspection_receipts(
        inspection_verdict_paths,
        released_pair_ids=frozenset(
            str(cast(Mapping[str, Any], item["sidecar"])["pair_id"]) for item in kept.rows
        ),
    )
    screen_by_name = {
        str(item.get("name")): item
        for item in cast(Sequence[Mapping[str, Any]], screens.get("screens") or [])
    }
    operations = _count_by(
        [cast(Mapping[str, Any], item["sidecar"]) for item in kept.rows], "operation_id"
    )
    mechanisms = _count_by(
        [cast(Mapping[str, Any], item["sidecar"]) for item in kept.rows], "mechanism"
    )
    labels = {
        "positive": sum(bool(cast(Mapping[str, Any], item["row"])["label"]) for item in kept.rows),
        "negative": sum(
            not bool(cast(Mapping[str, Any], item["row"])["label"]) for item in kept.rows
        ),
    }
    source_retained_files = [
        {
            "source_key": receipt["source_key"],
            "run_id": receipt["run_id"],
            "project_id": receipt["project_id"],
            "path": str(Path(str(receipt["run_dir"])) / "retained.jsonl"),
            "sha256": cast(Mapping[str, Any], receipt["input_sha256"])["retained"],
        }
        for receipt in receipt_documents
    ]
    cache_sources: dict[tuple[str, str], dict[str, Any]] = {}
    for item in kept.rows:
        source = cast(Mapping[str, Any], item["_release_source"])
        identity = (str(source["source_key"]), str(item["cache_record_path"]))
        cache_sources.setdefault(
            identity,
            {
                "source_key": source["source_key"],
                "path": item["cache_record_path"],
                "sha256": item["cache_record_file_sha256"],
                "content_sha256": hash_canonical(item["cache_record"]),
            },
        )
    screen_names_pass = all(
        screen_by_name.get(name, {}).get("passed") is True
        for name in ("candidate_only", "reference_only", "family_held_out")
    )
    base_checks = {
        "nonempty": bool(kept.rows),
        "all_source_runs_authorized": all(
            all(value is True for value in cast(Mapping[str, Any], receipt["checks"]).values())
            for receipt in receipts
        ),
        "forced_resume_observed": any(
            receipt.get("forced_resume_observed") is True for receipt in receipts
        ),
        "exact_three_source_projects": projects == WAVE4_PROJECTS,
        "released_rows_cover_three_projects": set(released_projects) == WAVE4_PROJECTS,
        "exact_model_row_contract": all(
            set(cast(Mapping[str, Any], item["row"])) == {"reference", "candidate", "label"}
            for item in kept.rows
        ),
        "exact_certificate_closure": not closure_issues,
        "zero_n19": not any(
            group.operation_id == "N19_WHOLE_CLAIM_NEGATION_V1" for group in kept.groups
        ),
        "n25_released_share_capped": cast(int, negative_share["operation_selected_row_count"])
        <= cast(int, negative_share["maximum_operation_row_count"]),
        "zero_self_pairs": all(
            cast(Mapping[str, Any], item["row"])["reference"]
            != cast(Mapping[str, Any], item["row"])["candidate"]
            for item in kept.rows
        ),
        "zero_duplicate_physical_pairs": len(kept.rows)
        == len({str(cast(Mapping[str, Any], item["sidecar"])["pair_id"]) for item in kept.rows}),
        "shortcut_screens": screen_names_pass,
        "pair_delta_cells_balanced_after_lean_free_selection": pair_delta_balance.get("passed")
        is True,
        "provenance_consistent": provenance["consistent"] is True,
        "all_shards_complete": all(
            shard["complete"] is True and shard["finalized"] is True for shard in shard_manifests
        ),
        "clean_release_builder": builder["dirty"] is False,
    }
    if gate_200:
        base_checks["exactly_200_ancestry_roots"] = ancestry_roots == 200
        base_checks["manual_inspection"] = inspections["passed"] is True
    else:
        base_checks["passed_200_root_composition_gate"] = gate_source is not None
    aggregates = _wave4_group_aggregates(kept.groups)
    manifest = {
        "schema_version": WAVE4_RELEASE_SCHEMA,
        "release_id": release_id,
        "release_mode": "composition_gate_200" if gate_200 else "full",
        "row_schema": ROW_SCHEMA,
        "row_fields": ["reference", "candidate", "label"],
        "closure_schema": "sft1_wave4_shared_edge_closure_v1",
        "view": label,
        "source_runs": receipt_documents,
        "source_receipts_sha256": hash_canonical(receipt_documents),
        "source_retained_paths": [item["path"] for item in source_retained_files],
        "source_retained_files": source_retained_files,
        "source_cache_files": [cache_sources[key] for key in sorted(cache_sources)],
        "policy_hash": loaded.policy.policy_hash,
        "maximum_rows": maximum_rows,
        "input_physical_rows": selection.input_physical_rows,
        "input_logical_groups": selection.input_group_count,
        "screen_rejections": {
            "count": len(rejection_records),
            "by_reason": _count_by(rejection_records, "reason"),
            "file": "screen_rejections.json",
            "sha256": sha256_hex(rejection_bytes),
        },
        "capacity_dropped_groups": {
            "count": len(selection.capacity_dropped_group_ids),
            "file": "capacity_dropped_groups.json",
            "sha256": sha256_hex(capacity_bytes),
        },
        "negative_share_cap": negative_share,
        "pair_delta_balance": pair_delta_balance,
        "retained_rows": len(kept.rows),
        "logical_groups": len(kept.groups),
        "logical_rows": kept.logical_row_count,
        "roots": ancestry_roots,
        "labels": labels,
        "operations": operations,
        "mechanisms": mechanisms,
        "projects": dict(sorted(released_projects.items())),
        **aggregates,
        "shard_size": config.output.shard_size,
        "shards": shard_manifests,
        "cache_snapshots": snapshots,
        "shortcut_screens": {
            "file": "shortcut_screens.json",
            "sha256": sha256_hex(screen_bytes),
        },
        "pairwise_diagnostics": {
            "file": "pairwise_diagnostics.json",
            "sha256": sha256_hex(diagnostic_bytes),
        },
        "composition_gate": gate_source,
        "manual_inspection": inspections,
        "config_semantic_hash": runtime.config_hash,
        "gold_blocklist_sha256": gold.sha256,
        "provenance": provenance,
        "multiple_project_pins_allowed": True,
        "release_builder": builder,
        "finalized": True,
        "artifact_status": (
            "wave4_composition_gate_candidate"
            if gate_200
            else "wave4_composed_release_high_confidence"
        ),
    }
    write_atomic(out / "manifest.json", canonical_json_bytes(manifest) + b"\n")

    from leanfaith.sft1.sprint.integrity import validate_view

    integrity = validate_view(
        repo_root=repo_root,
        staging_root=out.parent,
        run_id=release_id,
        compacted_dir=out,
        source_runs=receipt_documents,
    )
    if integrity["passed"] is not True:
        raise SquareError("Wave 4 integrity failed: " + "; ".join(integrity["issues"][:20]))
    base_checks["integrity_report"] = True
    manifest_sha256 = hash_file(out / "manifest.json")
    integrity_sha256 = hash_file(out / "integrity_report.json")
    gate_report: dict[str, Any] | None = None
    if gate_200:
        gate_checks = {
            "exactly_200_ancestry_roots": ancestry_roots == 200,
            "all_source_runs_replayed_without_calls": base_checks["all_source_runs_authorized"],
            "forced_resume_observed": base_checks["forced_resume_observed"],
            "exact_certificate_closure": base_checks["exact_certificate_closure"],
            "all_four_logical_roles": kept.logical_row_count == len(kept.groups) * len(EDGE_ROLES),
            "manual_inspection": inspections["passed"] is True,
            "shortcut_screens": base_checks["shortcut_screens"],
            "pair_delta_cells_balanced": base_checks[
                "pair_delta_cells_balanced_after_lean_free_selection"
            ],
            "integrity": integrity["passed"] is True,
            "zero_n19": base_checks["zero_n19"],
            "n25_cap": base_checks["n25_released_share_capped"],
        }
        gate_id = "wave4_gate:" + hash_canonical(
            [release_id, manifest_sha256, integrity_sha256, inspections]
        )
        gate_report = {
            "schema_version": WAVE4_COMPOSITION_GATE_SCHEMA,
            "kind": "sft1_wave4_composition_gate_v1",
            "gate_id": gate_id,
            "release_id": release_id,
            "policy_hash": loaded.policy.policy_hash,
            "unique_ancestry_roots": ancestry_roots,
            "physical_rows": len(kept.rows),
            "logical_groups": len(kept.groups),
            "logical_rows": kept.logical_row_count,
            "source_receipts_sha256": hash_canonical(receipt_documents),
            "source_runs": [
                {
                    "source_key": receipt["source_key"],
                    "run_id": receipt["run_id"],
                    "project_id": receipt["project_id"],
                    "input_sha256": receipt["input_sha256"],
                    "checks": receipt["checks"],
                }
                for receipt in receipt_documents
            ],
            "manual_inspection": inspections,
            "manifest_sha256": manifest_sha256,
            "integrity_report_sha256": integrity_sha256,
            "shortcut_screens_sha256": sha256_hex(screen_bytes),
            "checks": gate_checks,
            "passed": all(gate_checks.values()),
        }
        gate_report["content_binding_sha256"] = hash_canonical(gate_report)
        write_atomic(
            out / "composition_gate_report.json", canonical_json_bytes(gate_report) + b"\n"
        )
    report = {
        "schema_version": WAVE4_RELEASE_SCHEMA,
        "release_id": release_id,
        "view": label,
        "release_mode": manifest["release_mode"],
        "artifact_status": manifest["artifact_status"],
        "manifest_sha256": manifest_sha256,
        "integrity_report_sha256": integrity_sha256,
        "composition_gate_report_sha256": (
            hash_file(out / "composition_gate_report.json")
            if (out / "composition_gate_report.json").is_file()
            else None
        ),
        "physical_rows": len(kept.rows),
        "logical_groups": len(kept.groups),
        "logical_rows": kept.logical_row_count,
        **aggregates,
        "projects": dict(sorted(released_projects.items())),
        "source_runs": len(receipt_documents),
        "screen_sample": sample_receipt,
        "shortcut": screens,
        "manual_inspection": inspections,
        "pairwise_diagnostics": pair_delta,
        "outer_negation_xor_baseline": xor_baseline,
        "composition_gate": gate_source,
        "checks": base_checks,
        "passed": all(base_checks.values()),
    }
    report["content_binding_sha256"] = hash_canonical(
        {
            "manifest_sha256": manifest_sha256,
            "integrity_report_sha256": integrity_sha256,
            "composition_gate_report_sha256": report["composition_gate_report_sha256"],
            "checks": base_checks,
            "pairwise_diagnostics": pair_delta,
            "shortcut": screens,
            "manual_inspection": inspections,
        }
    )
    write_atomic(out / "release_report.json", canonical_json_bytes(report) + b"\n")
    if gate_report is not None and gate_report["passed"] is not True:
        raise SquareError("Wave 4 200-root composition gate did not pass")
    return report


def build_wave4_view(
    repo_root: Path,
    loaded: LoadedWave4Config,
    *,
    run_ids: Sequence[str],
    label: str,
    maximum_rows: int | None = None,
    allow_multiple_project_pins: bool = False,
) -> dict[str, Any]:
    """Compact Wave 4 runs as shared-edge certificate closures, never legacy squares."""

    from leanfaith.sft1.sprint import shortcut

    runtime = loaded.runtime
    config = runtime.config
    staging = Path(config.output.staging_root)
    out = staging / "compacted" / label
    if out.exists():
        raise SquareError(f"{out} already exists; Wave 4 views are additive and immutable")
    if not run_ids:
        raise SquareError("Wave 4 build needs at least one source run")

    rows_by_pair: dict[str, dict[str, Any]] = {}
    groups_by_id: dict[str, dict[str, Any]] = {}
    source_retained_paths: list[str] = []
    for run_id in run_ids:
        paths = RunPaths(staging, run_id)
        manifest = read_json_object(paths.run_manifest)
        if manifest.get("runner_kind") != WAVE4_CACHE_KIND:
            raise SquareError(f"run {run_id!r} is not a Wave 4 orbit run")
        if manifest.get("wave4_policy_hash") != loaded.policy.policy_hash:
            raise SquareError(f"run {run_id!r} has a different Wave 4 policy")
        bundle = load_wave4_retained(paths)
        attached = attach_cache_records(list(bundle.rows), staging / "cache")
        for row in attached:
            pair_id = str(cast(Mapping[str, Any], row["sidecar"])["pair_id"])
            previous = rows_by_pair.get(pair_id)
            if previous is not None and previous != row:
                raise SquareError(f"Wave 4 pair {pair_id} conflicts across source runs")
            rows_by_pair.setdefault(pair_id, row)
        for group in bundle.groups:
            previous_group = groups_by_id.get(group.group_id)
            if previous_group is not None and previous_group != group.record:
                raise SquareError(f"Wave 4 group {group.group_id} conflicts across source runs")
            groups_by_id.setdefault(group.group_id, group.record)
        source_retained_paths.append(str(paths.retained.relative_to(staging)))
    materialized = materialize_wave4_records(
        list(rows_by_pair.values()), list(groups_by_id.values())
    )

    gold = GoldBlocklist.load(
        repo_root / config.screens.gold_blocklist_path,
        expected_sha256=config.screens.gold_blocklist_sha256,
    )
    rejected_pairs: dict[str, str] = {}
    for record in materialized.rows:
        model_row = cast(Mapping[str, Any], record["row"])
        reason = (
            residue_violation(str(model_row["reference"]))
            or residue_violation(str(model_row["candidate"]))
            or ("self_pair_text" if model_row["reference"] == model_row["candidate"] else None)
            or (
                "gold_blocklist"
                if gold.hit(str(model_row["reference"])) or gold.hit(str(model_row["candidate"]))
                else None
            )
        )
        if reason:
            pair_id = str(cast(Mapping[str, Any], record["sidecar"])["pair_id"])
            rejected_pairs[pair_id] = reason
    screened_groups = [
        group
        for group in materialized.groups
        if not set(group.row_ids).intersection(rejected_pairs)
    ]
    screened = _rematerialize_wave4_selection(materialized, screened_groups)

    negative_families = _wave4_mapping(
        loaded.raw.get("negative_families"), "wave4.config.negative_families"
    )
    shares = _wave4_mapping(
        negative_families.get("maximum_released_share"),
        "wave4.config.negative_families.maximum_released_share",
    )
    n25_share = shares.get("N25_TOGGLE_EQ_NE_PROOF_V1")
    if isinstance(n25_share, bool) or not isinstance(n25_share, int | float):
        raise OrbitError("Wave 4 N25 maximum released share must be numeric")
    selection = select_wave4_release_groups(
        screened,
        maximum_rows=maximum_rows,
        n25_maximum_share=float(n25_share),
        selection_salt=loaded.policy.selection_salt,
        enforce_pair_delta_balance=True,
    )
    kept = selection.materialized
    if not kept.rows:
        raise SquareError("Wave 4 compaction retained no certified closure rows")

    shards = _wave4_release_shards(kept, config.output.shard_size, loaded.policy.selection_salt)
    out.mkdir(parents=True)
    screen_rejection_records = [
        {"pair_id": pair_id, "reason": reason} for pair_id, reason in sorted(rejected_pairs.items())
    ]
    screen_rejection_bytes = canonical_json_bytes(screen_rejection_records) + b"\n"
    write_atomic(out / "screen_rejections.json", screen_rejection_bytes)
    capacity_drop_bytes = canonical_json_bytes(list(selection.capacity_dropped_group_ids)) + b"\n"
    write_atomic(out / "capacity_dropped_groups.json", capacity_drop_bytes)
    negative_share = dict(selection.negative_share_report)
    share_dropped = cast(list[str], negative_share.pop("dropped_group_ids"))
    share_drop_bytes = canonical_json_bytes(share_dropped) + b"\n"
    write_atomic(out / "negative_share_dropped_groups.json", share_drop_bytes)
    negative_share["dropped_group_ids_count"] = len(share_dropped)
    negative_share["dropped_group_ids_sha256"] = sha256_hex(share_drop_bytes)
    negative_share["dropped_group_ids_file"] = "negative_share_dropped_groups.json"
    pair_delta_balance = dict(selection.pair_delta_balance_report)
    pair_delta_dropped = cast(list[str], pair_delta_balance.pop("quarantined_group_ids"))
    pair_delta_drop_bytes = canonical_json_bytes(pair_delta_dropped) + b"\n"
    write_atomic(out / "pair_delta_quarantined_groups.json", pair_delta_drop_bytes)
    pair_delta_balance.update(
        {
            "quarantined_group_ids_count": len(pair_delta_dropped),
            "quarantined_group_ids_sha256": sha256_hex(pair_delta_drop_bytes),
            "quarantined_group_ids_file": "pair_delta_quarantined_groups.json",
        }
    )
    snapshot_manifests = write_cache_snapshots(out, [rows for rows, _groups in shards])
    provenance = derive_provenance(
        kept.rows,
        repo_root=repo_root,
        cache_root=staging / "cache",
        release_dir=out,
        allow_multiple_project_pins=allow_multiple_project_pins,
    )
    if not provenance["consistent"]:
        shutil.rmtree(out)
        raise SquareError("Wave 4 provenance inconsistent: " + "; ".join(provenance["issues"]))

    shard_manifests: list[dict[str, Any]] = []
    for number, (rows, groups) in enumerate(shards, start=1):
        shard_dir = out / f"shard-{number:04d}"
        shard_dir.mkdir()
        rows_bytes = b"".join(canonical_json_bytes(item["row"]) + b"\n" for item in rows)
        sidecars_bytes = b"".join(canonical_json_bytes(item["sidecar"]) + b"\n" for item in rows)
        groups_bytes = b"".join(canonical_json_bytes(group.record) + b"\n" for group in groups)
        write_atomic(shard_dir / "rows.jsonl", rows_bytes)
        write_atomic(shard_dir / "sidecars.jsonl", sidecars_bytes)
        write_atomic(shard_dir / "closure_groups.jsonl", groups_bytes)
        shard_manifest = {
            "schema_version": 1,
            "row_schema": ROW_SCHEMA,
            "closure_schema": "sft1_wave4_shared_edge_closure_v1",
            "view": label,
            "shard": number,
            "row_count": len(rows),
            "logical_group_count": len(groups),
            "logical_row_count": len(groups) * len(EDGE_ROLES),
            "roots": len({group.root_id for group in groups}),
            "rows_sha256": sha256_hex(rows_bytes),
            "sidecars_sha256": sha256_hex(sidecars_bytes),
            "closure_groups_sha256": sha256_hex(groups_bytes),
            "complete": True,
            "finalized": True,
        }
        write_atomic(shard_dir / "manifest.json", canonical_json_bytes(shard_manifest) + b"\n")
        shard_manifests.append(shard_manifest)

    rows_for_diagnostics = [{"row": item["row"]} for item in kept.rows]
    pair_delta = shortcut.pairwise_shortcut_diagnostics(rows_for_diagnostics)
    xor_baseline = shortcut.outer_negation_xor_baseline(rows_for_diagnostics)
    write_atomic(out / "pairwise_diagnostics.json", canonical_json_bytes(pair_delta) + b"\n")
    write_atomic(
        out / "outer_negation_xor_baseline.json", canonical_json_bytes(xor_baseline) + b"\n"
    )
    sample, screen_sample_info = shortcut.screen_sample(out)
    screens = shortcut.run_screens_v3(sample)
    control = shortcut.permutation_control(sample)
    write_atomic(out / "permutation_control.json", canonical_json_bytes(control) + b"\n")
    screen_by_name = {
        str(result["name"]): result for result in cast(list[dict[str, Any]], screens["screens"])
    }
    unchecked = 0
    for record in kept.rows:
        model_row = cast(Mapping[str, Any], record["row"])
        evidence = cast(Mapping[str, Any], cast(Mapping[str, Any], record["sidecar"])["evidence"])
        check = (
            cast(Mapping[str, Any], evidence.get("equivalence_proof") or {}).get("check")
            if model_row["label"]
            else cast(Mapping[str, Any], evidence.get("refutation") or {}).get("check")
        )
        if not isinstance(check, Mapping) or not (
            check.get("meta_checked") is True and check.get("kernel_checked") is True
        ):
            unchecked += 1
    checks = {
        "nonempty": bool(kept.rows),
        "exact_model_row_contract": all(
            set(cast(Mapping[str, Any], item["row"])) == {"reference", "candidate", "label"}
            for item in kept.rows
        ),
        "zero_partial_groups": kept.logical_row_count == len(kept.groups) * len(EDGE_ROLES),
        "zero_duplicate_pair_ids": len(
            {str(cast(Mapping[str, Any], item["sidecar"])["pair_id"]) for item in kept.rows}
        )
        == len(kept.rows),
        "all_rows_kernel_and_meta_checked_at_generation": unchecked == 0,
        "n25_released_share_capped": cast(int, negative_share["operation_selected_row_count"])
        <= cast(int, negative_share["maximum_operation_row_count"]),
        "candidate_only_screen": bool(screen_by_name["candidate_only"]["passed"]),
        "reference_only_screen": bool(screen_by_name["reference_only"]["passed"]),
        "family_held_out_screen": bool(screen_by_name["family_held_out"]["passed"]),
        "pair_delta_cells_balanced_after_lean_free_selection": pair_delta_balance.get("passed")
        is True,
        "provenance_consistent": bool(provenance["consistent"]),
        "all_shards_complete": all(bool(shard["complete"]) for shard in shard_manifests),
    }
    aggregates = _wave4_group_aggregates(kept.groups)
    manifest = {
        "schema_version": 1,
        "row_schema": ROW_SCHEMA,
        "row_fields": ["reference", "candidate", "label"],
        "closure_schema": "sft1_wave4_shared_edge_closure_v1",
        "view": label,
        "source_runs": list(run_ids),
        "source_retained_paths": source_retained_paths,
        "policy_hash": loaded.policy.policy_hash,
        "maximum_rows": maximum_rows,
        "input_physical_rows": selection.input_physical_rows,
        "input_logical_groups": selection.input_group_count,
        "screen_rejections": {
            "count": len(screen_rejection_records),
            "by_reason": _count_by(screen_rejection_records, "reason"),
            "file": "screen_rejections.json",
            "sha256": sha256_hex(screen_rejection_bytes),
        },
        "capacity_dropped_groups": {
            "count": len(selection.capacity_dropped_group_ids),
            "file": "capacity_dropped_groups.json",
            "sha256": sha256_hex(capacity_drop_bytes),
        },
        "negative_share_cap": negative_share,
        "pair_delta_balance": pair_delta_balance,
        "retained_rows": len(kept.rows),
        "logical_groups": len(kept.groups),
        "logical_rows": kept.logical_row_count,
        **aggregates,
        "shard_size": config.output.shard_size,
        "shards": shard_manifests,
        "cache_snapshots": snapshot_manifests,
        "config_semantic_hash": runtime.config_hash,
        "gold_blocklist_sha256": gold.sha256,
        "provenance": provenance,
        "finalized": True,
        "artifact_status": (
            "wave4_composed_release_high_confidence"
            if all(checks.values())
            else "candidate_wave4_release_gate_failed"
        ),
    }
    report = {
        "schema_version": 1,
        "view": label,
        "generated_at": utc_now(),
        "physical_rows": len(kept.rows),
        "logical_groups": len(kept.groups),
        "logical_rows": kept.logical_row_count,
        **aggregates,
        "screen_sample": screen_sample_info,
        "shortcut": screens,
        "permutation_control": {key: value for key, value in control.items() if key != "per_seed"},
        "pairwise_diagnostics": pair_delta,
        "outer_negation_xor_baseline": xor_baseline,
        "checks": checks,
        "passed": all(checks.values()),
    }
    write_atomic(out / "manifest.json", canonical_json_bytes(manifest) + b"\n")
    write_atomic(out / "release_report.json", canonical_json_bytes(report) + b"\n")
    return report


def build_square_view(
    repo_root: Path,
    loaded: LoadedConfig[SprintConfig],
    *,
    run_ids: Sequence[str],
    label: str = "core_v3_square",
    regenerate: bool = True,
    supersedes: str | None = None,
    require_identical_rows: str | None = None,
    preferred_operations: Sequence[str] = (),
    allow_multiple_project_pins: bool = False,
    maximum_rows: int | None = None,
) -> dict[str, Any]:
    from leanfaith.sft1.sprint import shortcut

    config = loaded.config
    staging = Path(config.output.staging_root)
    out = staging / "compacted" / label
    if out.exists():
        raise SquareError(f"{out} already exists; square views are additive and immutable")
    quarantined: list[dict[str, Any]] = []
    source_retained_paths: list[str] = []
    records: list[dict[str, Any]] = []
    regenerated: list[tuple[Path, list[dict[str, Any]]]] = []
    recovered_roots: list[str] = []
    for run_id in run_ids:
        paths = RunPaths(staging, run_id)
        run_manifest = read_json_object(paths.run_manifest)
        operation_id = str(run_manifest.get("operation_id") or SQUARE_OPERATION)
        if regenerate:
            census = read_json_object(census_path_for(staging, operation_id))
            census_roots = cast(list[dict[str, Any]], census["roots"])
            runner = SquareRunner(
                repo_root,
                loaded,
                run_id=run_id,
                roots=census_roots,
                operation_id=operation_id,
                # records of an existing run live under the key schema that run used
                cache_schema=int(run_manifest.get("cache_schema", SQUARE_CACHE_SCHEMA_LEGACY)),
            )
            runner.load_state()
            run_records, run_quarantined = regenerate_records(
                runner,
                raw_dir=paths.raw,
                roots_by_name={str(item["name"]): item for item in census_roots},
            )
            regenerated_path = paths.run_dir / f"retained_{label}.jsonl"
            if regenerated_path.exists():
                raise SquareError(
                    f"{regenerated_path} already exists; regenerated files are additive"
                )
            regenerated.append((regenerated_path, run_records))
            recovered_roots.extend(runner.recovered_roots)
            records.extend(run_records)
            quarantined.extend({**item, "run_id": run_id} for item in run_quarantined)
            source_retained_paths.append(str(regenerated_path.relative_to(staging)))
        else:
            records.extend(attach_cache_records(load_square_retained(paths), staging / "cache"))
            source_retained_paths.append(str(paths.retained.relative_to(staging)))
    input_records = len(records)
    records, repeated_input_records, provenance_only_repeated_records = (
        collapse_exact_repeated_records(records)
    )
    gold = GoldBlocklist.load(
        repo_root / config.screens.gold_blocklist_path,
        expected_sha256=config.screens.gold_blocklist_sha256,
    )
    screened: list[dict[str, Any]] = []
    rejections: dict[str, int] = {}
    for record in records:
        row = record["row"]
        reason = (
            residue_violation(str(row["reference"]))
            or residue_violation(str(row["candidate"]))
            or ("self_pair_text" if row["reference"] == row["candidate"] else None)
            or (
                "gold_blocklist"
                if gold.hit(str(row["reference"])) or gold.hit(str(row["candidate"]))
                else None
            )
        )
        if reason:
            rejections[reason] = rejections.get(reason, 0) + 1
        else:
            screened.append(record)
    outcome = deduplicate(screened)  # conflict detection: same unordered pair, different labels
    selection = cap_square_selection(
        select_squares(screened, outcome.conflict_keys, preferred_operations), maximum_rows
    )
    kept = selection.kept
    complete_roots = set(selection.accepted_roots)  # square keys (root|operation)
    by_root: dict[str, list[dict[str, Any]]] = {}
    for record in kept:
        by_root.setdefault(square_key(record["sidecar"]), []).append(record)
    screened_by_root: dict[str, int] = {}
    root_names: dict[str, str] = {}
    for record in screened:
        key = square_key(record["sidecar"])
        screened_by_root[key] = screened_by_root.get(key, 0) + 1
        root_names[key] = str(record["sidecar"].get("root_name"))
    duplicate_rows = sum(screened_by_root[d["square"]] for d in selection.duplicate_squares)
    degenerate_rows = sum(screened_by_root[root] for root in selection.degenerate_roots)
    superseded_rows = sum(screened_by_root[d["square"]] for d in selection.superseded_squares)
    capacity_rows = sum(screened_by_root[root] for root in selection.capacity_squares)
    distinct_roots = {str(record["sidecar"]["root_id"]) for record in kept}
    conservation = {
        "screened_rows": len(screened),
        "kept_rows": len(kept),
        "duplicate_square_rows_dropped": duplicate_rows,
        "degenerate_square_rows_dropped": degenerate_rows,
        "superseded_square_rows_dropped": superseded_rows,
        "capacity_square_rows_dropped": capacity_rows,
        "holds": len(screened)
        == len(kept) + duplicate_rows + degenerate_rows + superseded_rows + capacity_rows,
    }
    incomplete = len(selection.degenerate_roots)
    conflicting_rows = selection.conflict_rows
    size = config.output.shard_size
    shards: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_root: str | None = None
    for record in kept:
        root = str(record["sidecar"]["root_id"])
        if current and len(current) >= size and root != current_root:
            shards.append(current)
            current = []
        current.append(record)
        current_root = root
    if current:
        shards.append(current)
    out.mkdir(parents=True)
    # immutable release evidence: every referenced cache record, content-addressed and
    # packed per shard; sidecars point at their snapshot line
    snapshot_manifests = write_cache_snapshots(out, shards)
    provenance = derive_provenance(
        kept,
        repo_root=repo_root,
        cache_root=Path(config.output.staging_root) / "cache",
        release_dir=out,
        allow_multiple_project_pins=allow_multiple_project_pins,
    )
    if not provenance["consistent"]:
        shutil.rmtree(out)
        raise SquareError("provenance inconsistent: " + "; ".join(provenance["issues"]))
    for regenerated_path, run_records in regenerated:
        write_atomic(
            regenerated_path,
            b"".join(
                canonical_json_bytes({k: v for k, v in item.items() if k != "cache_record"}) + b"\n"
                for item in run_records
            ),
        )
    shard_manifests: list[dict[str, Any]] = []
    for number, shard in enumerate(shards, start=1):
        shard_dir = out / f"shard-{number:04d}"
        shard_dir.mkdir()
        rows_bytes = b"".join(canonical_json_bytes(item["row"]) + b"\n" for item in shard)
        sidecar_bytes = b"".join(canonical_json_bytes(item["sidecar"]) + b"\n" for item in shard)
        write_atomic(shard_dir / "rows.jsonl", rows_bytes)
        write_atomic(shard_dir / "sidecars.jsonl", sidecar_bytes)
        manifest: dict[str, Any] = {
            "schema_version": 2,
            "row_schema": ROW_SCHEMA,
            "view": label,
            "shard": number,
            "row_count": len(shard),
            "complete": True,
            "finalized": True,
            "labels": {
                "positive": sum(1 for item in shard if item["label"]),
                "negative": sum(1 for item in shard if not item["label"]),
            },
            "roots": len({item["sidecar"]["root_id"] for item in shard}),
            "rows_sha256": sha256_hex(rows_bytes),
            "sidecars_sha256": sha256_hex(sidecar_bytes),
            "engine_source_sha256_set": sorted(
                {str(item["sidecar"]["engine"]["source_sha256"]) for item in shard}
            ),
        }
        write_atomic(shard_dir / "manifest.json", canonical_json_bytes(manifest) + b"\n")
        shard_manifests.append(manifest)
    manifest = {
        "schema_version": 2,
        "row_schema": ROW_SCHEMA,
        "row_fields": ["reference", "candidate", "label"],
        "sprint_id": config.sprint_id,
        "run_id": label,
        "view": label,
        "source_runs": list(run_ids),
        "source_retained_paths": source_retained_paths,
        "regenerated_from_cache": regenerate,
        "quarantined_roots": len(quarantined),
        "supersedes": supersedes,
        "compacted_at": utc_now(),
        "finalized": True,
        "input_records": input_records,
        "unique_input_records": len(records),
        "repeated_input_records_dropped": repeated_input_records,
        "provenance_only_repeated_input_records_dropped": provenance_only_repeated_records,
        "screen_rejections": rejections,
        "duplicate_rows_seen": repeated_input_records + outcome.duplicate_count,
        "duplicate_squares_dropped": len(selection.duplicate_squares),
        "degenerate_squares_dropped": incomplete,
        "capacity_squares_dropped": len(selection.capacity_squares),
        "maximum_rows": maximum_rows,
        "conflicting_classes_rejected": outcome.conflict_count,
        "conflicting_rows_rejected": conflicting_rows,
        "conservation": conservation,
        "view_dropped": len(screened) - len(kept),
        "retained_rows": len(kept),
        "labels": {
            "positive": sum(1 for item in kept if item["label"]),
            "negative": sum(1 for item in kept if not item["label"]),
        },
        **sidecar_aggregates([item["sidecar"] for item in kept]),
        "cache_snapshots": snapshot_manifests,
        "roots": len(distinct_roots),
        "grouping": "four_rows_per_root_same_shard",
        "orientation_rule": "square_fixed_marginals",
        "shard_size": size,
        "shards": shard_manifests,
        "config_semantic_hash": loaded.config_hash,
        "multiple_project_pins_allowed": allow_multiple_project_pins,
        "provenance": provenance,
        "gold_blocklist_sha256": gold.sha256,
        "proof_check_time": "original_generation",
        "replay_semantics": "journal_and_cache_replay_of_stored_terminals_no_fresh_kernel_replay",
        "artifact_status": "candidate_square_release_pending_gate",
        "superseded_squares_dropped": len(selection.superseded_squares),
        "preferred_operations": list(preferred_operations),
        "recovered_roots": sorted(recovered_roots),
        "sampling_configs": curriculum_sampling_configs()
        if any(str(item["sidecar"]["operation_id"]) == "SQUARE_N19_CURRICULUM_V1" for item in kept)
        else None,
    }
    write_atomic(out / "manifest.json", canonical_json_bytes(manifest) + b"\n")
    write_atomic(
        out / "duplicate_squares.json",
        canonical_json_bytes(
            [
                {
                    **item,
                    "root_name": root_names.get(item["square"]),
                    "duplicate_of_name": root_names.get(item["duplicate_of"]),
                }
                for item in selection.duplicate_squares
            ]
        )
        + b"\n",
    )
    write_atomic(out / "quarantined_roots.json", canonical_json_bytes(quarantined) + b"\n")
    write_atomic(
        out / "superseded_squares.json",
        canonical_json_bytes(
            [
                {**item, "root_name": root_names.get(item["square"])}
                for item in selection.superseded_squares
            ]
        )
        + b"\n",
    )
    reconciliations = [
        cast(dict[str, Any], item["sidecar"])["square"].get("alpha_reconciliation") for item in kept
    ]
    reconciliation_summary = {
        "squares": len(complete_roots),
        "roots": len(distinct_roots),
        "rows_with_reconciliation": sum(1 for r in reconciliations if r),
        "rows_matched": sum(1 for r in reconciliations if r and r.get("matches")),
        "quarantined_roots": quarantined,
    }
    write_atomic(
        out / "alpha_reconciliation.json", canonical_json_bytes(reconciliation_summary) + b"\n"
    )
    identical_rows: dict[str, Any] | None = None
    if require_identical_rows is not None:
        reference_dir = staging / "compacted" / require_identical_rows
        reference_shards = sorted(reference_dir.glob("shard-*"))
        new_shards = sorted(out.glob("shard-*"))
        per_shard = []
        for a, b in zip(reference_shards, new_shards, strict=False):
            per_shard.append(
                {
                    "shard": b.name,
                    "identical": (a / "rows.jsonl").read_bytes() == (b / "rows.jsonl").read_bytes(),
                    "rows_sha256": hash_file(b / "rows.jsonl"),
                }
            )
        identical_rows = {
            "reference_label": require_identical_rows,
            "shard_count_equal": len(reference_shards) == len(new_shards),
            "shards": per_shard,
            "identical": len(reference_shards) == len(new_shards)
            and all(item["identical"] for item in per_shard),
        }
        write_atomic(out / "rows_identity.json", canonical_json_bytes(identical_rows) + b"\n")
    # full-view structural checks stream the shards; the model-facing rows alone feed the
    # cheap text diagnostics; the shortcut screens run on the whole view when it is small and
    # on a deterministic whole-root sample otherwise (recorded next to the results)
    row_only = [{"row": item["row"]} for item in shortcut.iter_serialized_view(out)]
    xor_baseline = shortcut.outer_negation_xor_baseline(row_only)
    write_atomic(
        out / "outer_negation_xor_baseline.json", canonical_json_bytes(xor_baseline) + b"\n"
    )
    diagnostics = shortcut.pairwise_shortcut_diagnostics(row_only)
    write_atomic(out / "pairwise_diagnostics.json", canonical_json_bytes(diagnostics) + b"\n")
    serialized_rows = len(row_only)
    exact_fields = all(set(item["row"]) == {"reference", "candidate", "label"} for item in row_only)
    del row_only
    sample, screen_sample_info = shortcut.screen_sample(out)
    screens = shortcut.run_screens_v3(sample)
    control = shortcut.permutation_control(sample)
    del sample
    write_atomic(out / "permutation_control.json", canonical_json_bytes(control) + b"\n")
    screen_by_name = {str(s["name"]): s for s in cast(list[dict[str, Any]], screens["screens"])}
    unchecked = 0
    marginal_ok = True
    positives_ref: dict[str, int] = {}
    negatives_ref: dict[str, int] = {}
    positives_cand: dict[str, int] = {}
    negatives_cand: dict[str, int] = {}
    for item in shortcut.iter_serialized_view(out):
        sidecar = item["sidecar"]
        evidence = sidecar.get("evidence") or {}
        check = (
            (evidence.get("equivalence_proof") or {}).get("check")
            if item["row"]["label"]
            else (evidence.get("refutation") or {}).get("check")
        )
        if not check or not check.get("meta_checked") or not check.get("kernel_checked"):
            unchecked += 1
        ref = str(item["row"]["reference"])
        cand = str(item["row"]["candidate"])
        if item["row"]["label"]:
            positives_ref[ref] = positives_ref.get(ref, 0) + 1
            positives_cand[cand] = positives_cand.get(cand, 0) + 1
        else:
            negatives_ref[ref] = negatives_ref.get(ref, 0) + 1
            negatives_cand[cand] = negatives_cand.get(cand, 0) + 1
    marginal_ok = positives_ref == negatives_ref and positives_cand == negatives_cand
    checks = {
        "square_nonempty": len(kept) > 0,
        "rows_are_exactly_reference_candidate_label": exact_fields,
        "four_rows_per_root": all(len(by_root[root]) == 4 for root in complete_roots)
        and len(kept) == 4 * len(complete_roots),
        "zero_incomplete_squares": incomplete == 0
        and all(len(items) == 4 for items in by_root.values()),
        "labels_balanced": manifest["labels"]["positive"] == manifest["labels"]["negative"],
        "identical_marginals_across_labels": marginal_ok,
        "all_rows_kernel_and_meta_checked_at_generation": unchecked == 0,
        "zero_duplicates": len({str(item["unordered_pair_key"]) for item in kept}) == len(kept),
        "conservation_holds": bool(conservation["holds"]),
        "zero_quarantined_roots": not quarantined,
        "alpha_reconciled_every_row": not regenerate
        or (
            reconciliation_summary["rows_with_reconciliation"] == len(kept)
            and reconciliation_summary["rows_matched"] == len(kept)
        ),
        "cache_records_verified": bool(provenance.get("consistent"))
        and int(provenance.get("square_cache_records_verified", 0)) == len(kept),
        "rows_byte_identical_to_reference": identical_rows is None
        or bool(identical_rows["identical"]),
        "zero_conflicts": outcome.conflict_count == 0 and conflicting_rows == 0,
        "finalized_shards_complete": all(bool(s["complete"]) for s in manifest["shards"]),
        "candidate_only_screen": bool(screen_by_name["candidate_only"]["passed"]),
        "reference_only_screen": bool(screen_by_name["reference_only"]["passed"]),
        "family_held_out_screen": bool(screen_by_name["family_held_out"]["passed"]),
    }
    report = {
        "schema_version": 1,
        "view": label,
        "generated_at": utc_now(),
        "source_runs": list(run_ids),
        "evaluated_on": "serialized_shards",
        "rows": serialized_rows,
        "screen_sample": screen_sample_info,
        "labels": manifest["labels"],
        "roots": len(complete_roots),
        "families": manifest["families"],
        "row_kinds": manifest["row_kinds"],
        "operations": manifest["operations"],
        "mechanisms": manifest["mechanisms"],
        "transforms": manifest["transforms"],
        "squares": manifest["squares"],
        "curriculum_only": manifest["curriculum_only"],
        "unchecked_rows": unchecked,
        "shortcut": screens,
        "permutation_control": {k: v for k, v in control.items() if k != "per_seed"},
        "outer_negation_xor_baseline": xor_baseline,
        "pairwise_diagnostics": diagnostics,
        "negative_mechanisms": manifest["negative_mechanisms"],
        "coverage_statement": (
            "high-confidence deterministic curriculum seed built from certificate-closure "
            "squares; not broad theorem-equivalence coverage"
        ),
        "proof_check_time": "original_generation",
        "replay_semantics": "journal_and_cache_replay_of_stored_terminals_no_fresh_kernel_replay",
        "checks": checks,
        "passed": all(checks.values()),
    }
    write_atomic(out / "release_report.json", canonical_json_bytes(report) + b"\n")
    if not report["passed"]:
        manifest["artifact_status"] = "candidate_square_release_gate_failed"
    elif manifest["curriculum_only"]:
        manifest["artifact_status"] = "curriculum_auxiliary_certified_easy_pattern"
    else:
        manifest["artifact_status"] = "square_release_high_confidence_curriculum_seed"
    write_atomic(out / "manifest.json", canonical_json_bytes(manifest) + b"\n")
    return report


# ------------------------------------------------------------------ fixtures


def run_square_fixtures(
    repo_root: Path,
    loaded: LoadedConfig[SprintConfig],
    fixtures: Sequence[Mapping[str, str]],
    operation_id: str = SQUARE_OPERATION,
) -> dict[str, Any]:
    if not fixtures:
        raise SquareError(f"no fixtures defined for {operation_id}")
    roots = [
        {"name": item["root"], "direction": "fixture", "reference_expr_hash": item["root"]}
        for item in fixtures
    ]
    pins = SprintRunner(repo_root, loaded, run_id="square-fixtures").identity
    short = operation_id.removeprefix("SQUARE_").removesuffix("_V1").lower()
    run_id = f"square-fixtures-{short}-{pins.source_sha256[:12]}"
    # Fixture gates always start fresh: they must exercise the engine, never resume a journal.
    stale_run_dir = RunPaths(Path(loaded.config.output.staging_root), run_id).run_dir
    if stale_run_dir.exists():
        shutil.rmtree(stale_run_dir)
    runner = SquareRunner(
        repo_root,
        loaded,
        run_id=run_id,
        roots=roots,
        use_cache=False,
        operation_id=operation_id,
        isolated_cache=True,
    )
    # the summary returned by run() carries the live Lean request accounting; calling
    # write_status again after the session closed would report zero requests
    summary = runner.run()
    terminals: dict[str, dict[str, Any]] = {}
    for record in runner.journal.read():
        if record.get("kind") == "square_terminal":
            terminals[str(record["root"])] = record
    results = []
    for item in fixtures:
        terminal = terminals.get(item["root"])
        status = None if terminal is None else str(terminal.get("status"))
        reason = "" if terminal is None else str(terminal.get("reason", ""))
        passed = status == item["expect_status"] and reason.startswith(
            item.get("expect_reason_prefix", "")
        )
        results.append(
            {
                "root": item["root"],
                "expect_status": item["expect_status"],
                "expect_reason_prefix": item.get("expect_reason_prefix", ""),
                "observed_status": status,
                "observed_reason": reason,
                "passed": passed,
            }
        )
    report = {
        "schema_version": 1,
        "run_id": run_id,
        "results": results,
        "retained_fixture_present": any(
            r["expect_status"] == "retained" and r["passed"] for r in results
        ),
        "rejection_fixture_present": any(
            r["expect_status"] != "retained" and r["passed"] for r in results
        ),
        "passed": all(r["passed"] for r in results)
        and any(r["expect_status"] == "retained" and r["passed"] for r in results)
        and any(r["expect_status"] != "retained" and r["passed"] for r in results),
        "status": summary,
    }
    write_atomic(
        runner.paths.run_dir / "fixtures_report.json", canonical_json_bytes(report) + b"\n"
    )
    return report


# ------------------------------------------------------------------ CLI

SQUARE_FIXTURES: dict[str, tuple[dict[str, str], ...]] = {
    SQUARE_OPERATION: (
        {"root": "Nat.mul_factorial_pred", "expect_status": "retained"},
        {
            "root": "PNat.gcd_comm",
            "expect_status": "rejected",
            "expect_reason_prefix": "no_ground_assignment",
        },
        {
            "root": "Nat.factorial_lt",
            "expect_status": "not_applicable",
            "expect_reason_prefix": "final_target_eq_ne_not_applicable",
        },
    ),
    "SQUARE_N25_BINDER_V1": (
        {"root": "Nat.gcd_fib_add_self", "expect_status": "retained"},  # P14 on (m n : Nat)
        {"root": "Nat.choose_eq_choose_pred_add", "expect_status": "retained"},  # P23 on hn hk
        {"root": "ZMod.prime_ne_zero", "expect_status": "retained"},  # P14 with Fact binders
        {
            "root": "PNat.gcd_comm",  # fail-closed: no binder transform applies
            "expect_status": "not_applicable",
            "expect_reason_prefix": "square_no_applicable_transform",
        },
        {
            "root": "Nat.factorial_succ",
            "expect_status": "not_applicable",
            "expect_reason_prefix": "square_no_applicable_transform",
        },
    ),
    "SQUARE_N19_CURRICULUM_V1": (
        {"root": "Nat.gcd_fib_add_self", "expect_status": "retained"},  # P14 under the negation
        {"root": "Nat.factorial_succ", "expect_status": "retained"},  # P18 under the negation
        {
            "root": "Nat.succ_pos'",
            "expect_status": "not_applicable",
            "expect_reason_prefix": "square_no_applicable_transform",
        },
    ),
    "SQUARE_N32_BINDER_V1": (
        {"root": "Nat.ascFactorial_pos", "expect_status": "retained"},  # P14 on (n k : Nat)
        {"root": "Nat.add_pred_div_lt", "expect_status": "retained"},  # P23 on hb hn
        {
            "root": "Nat.succ_pos'",
            "expect_status": "not_applicable",
            "expect_reason_prefix": "square_no_applicable_transform",
        },
        {
            "root": "Nat.factorial_succ",
            "expect_status": "not_applicable",
            "expect_reason_prefix": "final_target_strict_lt_not_applicable",
        },
    ),
    "SQUARE_WAVE2_N26_V1": (
        {"root": "Finset.mem_range", "expect_status": "retained"},
        {
            "root": "PNat.gcd_comm",
            "expect_status": "not_applicable",
            "expect_reason_prefix": "n26_no_finset_range_coverage_bound",
        },
    ),
    "SQUARE_WAVE2_N32_V1": (
        {"root": "Nat.add_factorial_succ_le_factorial_add_succ", "expect_status": "retained"},
        {
            "root": "PNat.gcd_comm",
            "expect_status": "not_applicable",
            "expect_reason_prefix": "final_target_strict_lt_not_applicable",
        },
    ),
    "SQUARE_WAVE2_N25_V1": (
        {"root": "Nat.factorization_factorial_mul_succ", "expect_status": "retained"},
        {
            "root": "PNat.gcd_comm",
            "expect_status": "not_applicable",
            "expect_reason_prefix": "square_no_applicable_transform",
        },
    ),
}


# ------------------------------------------------------------------ inspection
SQUARE_ROW_ORDER: dict[str, int] = {kind[0]: index for index, kind in enumerate(ROW_KINDS)}
SQUARE_ROW_ORDER.update({kind[0]: index for index, kind in enumerate(WAVE4_ROW_KINDS)})


def _check_flag(value: Any) -> str | None:
    """Summarise one certificate check object as MK (meta+kernel) or FAIL."""
    check = value.get("check", value) if isinstance(value, dict) else None
    if not isinstance(check, dict):
        return None
    meta = check.get("metaChecked", check.get("meta_checked"))
    kernel = check.get("kernelChecked", check.get("kernel_checked"))
    if meta is None and kernel is None:
        return None
    return "MK" if bool(meta) and bool(kernel) else "FAIL"


def square_inspection_lines(records: Sequence[Mapping[str, Any]]) -> list[str]:
    """Every retained legacy-square or Wave 4 physical row, grouped by root."""
    by_root: dict[str, list[Mapping[str, Any]]] = {}
    for record in records:
        by_root.setdefault(str(record["sidecar"]["root_name"]), []).append(record)
    lines = [
        "# SFT1 square inspection",
        "",
        f"- roots: {len(by_root)}",
        f"- rows: {len(records)}",
        "",
    ]
    for name in sorted(by_root):
        try:
            recs = sorted(
                by_root[name],
                key=lambda r: SQUARE_ROW_ORDER[str(r["sidecar"]["row_kind"])],
            )
        except KeyError as exc:
            raise SquareError(f"unknown square inspection row kind {exc.args[0]!r}") from exc
        first = cast(dict[str, Any], recs[0]["sidecar"])
        square = cast(dict[str, Any], first.get("square", {}))
        first_evidence = cast(dict[str, Any], first.get("evidence", {}))
        evidence = cast(dict[str, Any], first_evidence.get("square", first_evidence))
        flags = []
        for key, value in evidence.items():
            flag = _check_flag(value)
            if flag is not None:
                kind = value.get("kind") if isinstance(value, dict) else None
                flags.append(f"{key}:{flag}" + (f"({kind})" if kind else ""))
        metadata = (
            [
                f"- operation: {first.get('operation_id')}",
                f"- negative operation: {first.get('negative_operation')}",
                "- closure groups: "
                + str(
                    len(
                        {
                            str(group_id)
                            for record in recs
                            for group_id in cast(Mapping[str, Any], record["sidecar"]).get(
                                "closure_group_ids", []
                            )
                        }
                    )
                ),
            ]
            if first.get("row_kind") in WAVE4_ROW_LABEL
            else [
                f"- direction: {square.get('direction')} "
                f"(T_P={square.get('t_p')}, T_C={square.get('t_c')})"
            ]
        )
        lines += [
            f"## {name}",
            "",
            f"- module: `{first.get('module')}`",
            *metadata,
            f"- statement: `{first.get('statement')}`",
            f"- checks: {' '.join(flags) if flags else 'none recorded'}",
            "",
        ]
        for record in recs:
            row = cast(dict[str, Any], record["row"])
            kind = str(cast(dict[str, Any], record["sidecar"])["row_kind"])
            lines += [
                f"### {kind} (label {row['label']})",
                "",
                f"- reference: `{row['reference']}`",
                f"- candidate: `{row['candidate']}`",
                "",
            ]
    return lines


def write_square_inspection(paths: RunPaths, records: Sequence[Mapping[str, Any]]) -> Path:
    paths.inspection.mkdir(parents=True, exist_ok=True)
    out = paths.inspection / "sample.md"
    write_atomic(out, ("\n".join(square_inspection_lines(records)) + "\n").encode("utf-8"))
    return out


def select_stratified_audit_rows(
    records: Sequence[Mapping[str, Any]], maximum_rows: int
) -> list[dict[str, Any]]:
    """Select a stable max-min sample over source/operation/transform/row-kind cells."""
    if maximum_rows <= 0:
        raise SquareError("audit row count must be positive")
    buckets: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for item in records:
        record = dict(item)
        sidecar = cast(Mapping[str, Any], record["sidecar"])
        project = cast(Mapping[str, Any], sidecar.get("project") or {})
        square = cast(Mapping[str, Any], sidecar.get("square") or {})
        key = (
            str(project.get("project_id")),
            str(sidecar.get("operation_id")),
            str(square.get("t_p")),
            str(sidecar.get("row_kind")),
        )
        buckets.setdefault(key, []).append(record)
    for bucket in buckets.values():
        bucket.sort(
            key=lambda item: hash_canonical(
                ["sft1_wave2_audit_v1", cast(Mapping[str, Any], item["sidecar"])["pair_id"]]
            )
        )
    selected: list[dict[str, Any]] = []
    position = 0
    while len(selected) < min(maximum_rows, len(records)):
        advanced = False
        for key in sorted(buckets):
            bucket = buckets[key]
            if position < len(bucket):
                selected.append(bucket[position])
                advanced = True
                if len(selected) == min(maximum_rows, len(records)):
                    break
        if not advanced:
            break
        position += 1
    return selected


def write_stratified_audit(compacted_dir: Path, maximum_rows: int = 200) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    for shard in sorted(compacted_dir.glob("shard-*")):
        rows = [json.loads(line) for line in (shard / "rows.jsonl").read_text().splitlines()]
        sidecars = [
            json.loads(line) for line in (shard / "sidecars.jsonl").read_text().splitlines()
        ]
        if len(rows) != len(sidecars):
            raise SquareError(f"audit source row/sidecar mismatch: {shard}")
        records.extend(
            {"row": row, "sidecar": sidecar} for row, sidecar in zip(rows, sidecars, strict=True)
        )
    selected = select_stratified_audit_rows(records, maximum_rows)
    counts: dict[str, dict[str, int]] = {
        "sources": {},
        "operations": {},
        "transforms": {},
        "row_kinds": {},
    }
    rows_out: list[dict[str, Any]] = []
    markdown = [
        f"# {compacted_dir.name} deterministic stratified audit",
        "",
        f"- selected rows: {len(selected)}",
        "- inspection verdict: pending manual inspection",
        "",
    ]
    for index, item in enumerate(selected, start=1):
        row = cast(dict[str, Any], item["row"])
        sidecar = cast(dict[str, Any], item["sidecar"])
        project = cast(dict[str, Any], sidecar.get("project") or {})
        square = cast(dict[str, Any], sidecar.get("square") or {})
        source = str(project.get("project_id"))
        operation = str(sidecar.get("operation_id"))
        transform = str(square.get("t_p"))
        row_kind = str(sidecar.get("row_kind"))
        for name, value in (
            ("sources", source),
            ("operations", operation),
            ("transforms", transform),
            ("row_kinds", row_kind),
        ):
            counts[name][value] = counts[name].get(value, 0) + 1
        rows_out.append(
            {
                "index": index,
                "pair_id": sidecar.get("pair_id"),
                "root_name": sidecar.get("root_name"),
                "source": source,
                "operation_id": operation,
                "transform": transform,
                "row_kind": row_kind,
                "reference": row["reference"],
                "candidate": row["candidate"],
                "label": row["label"],
                "evidence_hash": sidecar.get("evidence_hash"),
            }
        )
        markdown.extend(
            [
                f"## {index}. {source} / {operation} / {transform} / {row_kind}",
                "",
                f"- root: `{sidecar.get('root_name')}`",
                f"- pair: `{sidecar.get('pair_id')}`",
                f"- label: `{row['label']}`",
                "- reference:",
                "```text",
                str(row["reference"]),
                "```",
                "- candidate:",
                "```text",
                str(row["candidate"]),
                "```",
                "",
            ]
        )
    report = {
        "schema_version": 1,
        "view": compacted_dir.name,
        "selection": (
            "stable max-min round-robin over project_id, operation_id, preserving transform, "
            "and row_kind; salted pair-id order within each cell"
        ),
        "requested_rows": maximum_rows,
        "selected_rows": len(selected),
        "selection_sha256": hash_canonical(
            [cast(Mapping[str, Any], item["sidecar"])["pair_id"] for item in selected]
        ),
        "counts": {name: dict(sorted(values.items())) for name, values in counts.items()},
        "inspection_status": "pending_manual",
        "rows": rows_out,
    }
    write_atomic(compacted_dir / "audit_200.json", canonical_json_bytes(report) + b"\n")
    write_atomic(compacted_dir / "audit_200.md", ("\n".join(markdown) + "\n").encode())
    return {key: value for key, value in report.items() if key != "rows"}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("census", "fixtures", "run", "replay", "build", "status", "inspect", "audit"),
    )
    parser.add_argument("--repo-root", type=Path, default=find_repo_root(Path.cwd()))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--run-id", default="square_full")
    parser.add_argument("--run-ids", help="comma-separated run ids for build (default: --run-id)")
    parser.add_argument(
        "--run-dir",
        action="append",
        type=Path,
        default=[],
        help="explicit completed Wave 4 run directory; repeat for every source run",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="new immutable Wave 4 release directory (required with --run-dir)",
    )
    parser.add_argument(
        "--gate-200",
        action="store_true",
        help="build the exact 200-root composition gate candidate",
    )
    parser.add_argument(
        "--inspection-verdict",
        action="append",
        type=Path,
        default=[],
        help="manual Wave 4 inspection receipt; repeat when needed",
    )
    parser.add_argument(
        "--composition-gate-report",
        type=Path,
        help="passed Wave 4 200-root gate report required by a full release",
    )
    parser.add_argument(
        "--source-run-ids",
        help="comma-separated certified negative run ids used only by census",
    )
    parser.add_argument(
        "--source-staging-root",
        type=Path,
        help="staging root containing --source-run-ids (required for cross-wave census)",
    )
    parser.add_argument(
        "--prefer-operations",
        help="comma-separated square operations that supersede other squares of the same root",
    )
    parser.add_argument("--operation", default=SQUARE_OPERATION, choices=sorted(SQUARE_OPERATIONS))
    parser.add_argument("--max-roots", type=int)
    parser.add_argument("--roots-file", type=Path, help="JSON target file with a roots list")
    parser.add_argument("--label", default="core_v3_square")
    parser.add_argument("--maximum-rows", type=int, help="stable whole-square released-row ceiling")
    parser.add_argument("--audit-rows", type=int, default=200)
    parser.add_argument("--owner-session", default="claude-sft1-square")
    parser.add_argument("--supersedes", help="label this view supersedes (recorded, not modified)")
    parser.add_argument(
        "--require-identical-rows",
        help="label whose model-facing rows.jsonl shards must be byte-identical",
    )
    parser.add_argument(
        "--no-regenerate",
        action="store_true",
        help="build from the run's retained file instead of regenerating from cache records",
    )
    parser.add_argument(
        "--allow-multiple-project-pins",
        action="store_true",
        help="admit independently pinned source projects in one combined square view",
    )
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()
    config_path = args.config.resolve() if args.config else None
    explicit_wave4_release = args.command == "build" and bool(args.run_dir)
    wave4_loaded = (
        load_wave4_config(repo_root, config_path)
        if explicit_wave4_release or args.operation in WAVE4_OPERATIONS
        else None
    )
    loaded = (
        wave4_loaded.runtime
        if wave4_loaded is not None
        else load_sprint_config(repo_root, config_path)
    )
    staging = Path(loaded.config.output.staging_root)
    if args.command == "census":
        source_run_ids = [
            value.strip()
            for value in (args.source_run_ids or ",".join(SOURCE_RUNS)).split(",")
            if value.strip()
        ]
        if wave4_loaded is not None and args.source_run_ids is None:
            raise SquareError(
                "Wave 4 census requires explicit --source-run-ids from certified Wave 3 runs"
            )
        report = write_census(
            loaded,
            census_path_for(staging, args.operation),
            args.operation,
            repo_root=repo_root,
            source_run_ids=source_run_ids,
            source_staging_root=(
                args.source_staging_root.resolve() if args.source_staging_root else None
            ),
        )
        print(json.dumps(report, indent=1))
        return 0
    if args.command == "fixtures":
        report = run_square_fixtures(
            repo_root, loaded, SQUARE_FIXTURES.get(args.operation, ()), args.operation
        )
        print(json.dumps({k: v for k, v in report.items() if k != "status"}, indent=1))
        return 0 if report["passed"] else 1
    if args.command in {"run", "replay"}:
        targets = (
            read_json_object(args.roots_file.resolve())
            if args.roots_file is not None
            else read_json_object(census_path_for(staging, args.operation))
        )
        roots = [
            (
                {"name": item, "direction": "target", "reference_expr_hash": item}
                if isinstance(item, str)
                else cast(dict[str, Any], item)
            )
            for item in cast(list[Any], targets["roots"])
        ]
        max_roots = args.max_roots
        manifest_path = RunPaths(staging, args.run_id).run_manifest
        if args.command == "replay" and manifest_path.is_file():
            recorded = read_json_object(manifest_path)
            if max_roots is None and isinstance(recorded.get("max_roots"), int):
                max_roots = int(recorded["max_roots"])
        if wave4_loaded is None:
            runner: SquareRunner = SquareRunner(
                repo_root,
                loaded,
                run_id=args.run_id,
                roots=roots,
                max_roots=max_roots,
                owner_session=args.owner_session,
                operation_id=args.operation,
            )
        else:
            runner = Wave4Runner(
                repo_root,
                loaded,
                policy=wave4_loaded.policy,
                run_id=args.run_id,
                roots=roots,
                max_roots=max_roots,
                owner_session=args.owner_session,
                operation_id=args.operation,
            )
        before = len(read_retained(runner.paths.retained))
        summary = runner.run(require_zero_lean=args.command == "replay")
        if args.command == "replay":
            report = {
                "run_id": args.run_id,
                "lean_requests": summary["lean_requests"],
                "duplicate_rows": len(read_retained(runner.paths.retained)) - before,
                "retained_before": before,
                "retained_after": len(read_retained(runner.paths.retained)),
                "roots_considered": summary["roots_considered"],
            }
            write_atomic(
                runner.paths.run_dir / "replay_report.json", canonical_json_bytes(report) + b"\n"
            )
            print(json.dumps(report))
            return 0
        print(json.dumps(summary, ensure_ascii=False))
        return 0
    if args.command == "status":
        print(json.dumps(read_json_object(RunPaths(staging, args.run_id).status), indent=1))
        return 0
    if args.command == "inspect":
        paths = RunPaths(staging, args.run_id)
        records = read_retained(paths.retained)
        out = write_square_inspection(paths, records)
        print(json.dumps({"run_id": args.run_id, "rows": len(records), "path": str(out)}))
        return 0
    if args.command == "audit":
        report = write_stratified_audit(staging / "compacted" / args.label, args.audit_rows)
        print(json.dumps(report, indent=1))
        return 0
    build_run_ids = [
        value.strip() for value in (args.run_ids or args.run_id).split(",") if value.strip()
    ]
    if explicit_wave4_release:
        if wave4_loaded is None or args.output_dir is None:
            raise SquareError("explicit Wave 4 build requires --output-dir")
        report = build_wave4_release(
            repo_root,
            wave4_loaded,
            run_dirs=args.run_dir,
            output_dir=args.output_dir,
            label=("wave4/composed_core_v1" if args.label == "core_v3_square" else args.label),
            maximum_rows=args.maximum_rows,
            gate_200=args.gate_200,
            inspection_verdict_paths=args.inspection_verdict,
            composition_gate_report=args.composition_gate_report,
        )
    elif wave4_loaded is not None:
        report = build_wave4_view(
            repo_root,
            wave4_loaded,
            run_ids=build_run_ids,
            label=args.label,
            maximum_rows=args.maximum_rows,
            allow_multiple_project_pins=args.allow_multiple_project_pins,
        )
    else:
        report = build_square_view(
            repo_root,
            loaded,
            run_ids=build_run_ids,
            label=args.label,
            regenerate=not args.no_regenerate,
            supersedes=args.supersedes,
            require_identical_rows=args.require_identical_rows,
            preferred_operations=[
                value.strip()
                for value in (args.prefer_operations or "").split(",")
                if value.strip()
            ],
            allow_multiple_project_pins=args.allow_multiple_project_pins,
            maximum_rows=args.maximum_rows,
        )
    print(
        json.dumps(
            {k: v for k, v in report.items() if k not in {"shortcut"}}, ensure_ascii=False, indent=1
        )
    )
    print(json.dumps(report["shortcut"]["screens"], indent=1))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
