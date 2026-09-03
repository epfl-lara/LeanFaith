"""Durable exact-certificate gate for the Wave 5 compiler audit sample.

This additive module keeps the large resumable gate separate from
``compiler_replay``, whose core responsibility is exact source reconstruction
and typed multi-root request/response validation.  All Lean work still uses
those shared primitives and one persistent central backend.
"""

from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, Protocol, cast

import yaml

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.config.paths import find_repo_root
from leanfaith.lean.leaninteract_backend import METHOD_VERSION as BACKEND_METHOD_VERSION
from leanfaith.lean.protocol import LeanRequest, LeanResult, LeanStatus
from leanfaith.lean.session_policy import RetryPolicy, run_batch_with_retries
from leanfaith.representations import goal_v1 as goal_v1_module
from leanfaith.representations.goal_v1 import (
    CompileContext,
    GoalV1Error,
    _implementation_identity,
    _messages_report_sorry,
)
from leanfaith.sft1.sprint import compiler_inventory as inventory_module
from leanfaith.sft1.sprint import compiler_replay as replay_module
from leanfaith.sft1.sprint import orbit as orbit_module
from leanfaith.sft1.sprint import runner as runner_module
from leanfaith.sft1.sprint import screens as screens_module
from leanfaith.sft1.sprint import square as square_module
from leanfaith.sft1.sprint.compiler_inventory import load_pinned_input_shards
from leanfaith.sft1.sprint.compiler_replay import (
    _NONRESULT_INFRASTRUCTURE,
    _SHA256,
    _TYPED_ENDPOINTS,
    CHECKER_VERSION,
    TYPED_HOOK_CHECKER_VERSION,
    BackendFactory,
    CompilerAuditSettings,
    CompilerAuditSource,
    CompilerReplayError,
    CompilerReplayInfrastructureError,
    CompilerTypedHookSpec,
    CompilerTypedWave4Selection,
    _backend_context_fingerprint,
    _backend_settings,
    _compiler_binding_json,
    _default_backend_factory,
    _mapping,
    _preflight_reason,
    _require_checked_source_proof,
    _require_exact_compiler_binding,
    _resolve_path,
    _resource_claim,
    _RssSampler,
    _string_list,
    _typed_compile_context,
    _typed_source_binding,
    _verify_project,
    _write_exact,
    build_typed_descriptor_batch_request,
    build_typed_wave4_selected_batch_request,
    load_audit_sample,
    load_compiler_audit_config,
    parse_typed_descriptor_batch_payloads,
    resolve_audit_sources,
    typed_wave4_endpoint_id,
    validate_typed_wave4_selected_batch_result,
)
from leanfaith.sft1.sprint.engine import (
    NEGATIVE_OPERATIONS,
    POSITIVE_OPERATIONS,
    SprintEngineError,
    engine_semantic_version,
    mechanism_of,
)
from leanfaith.sft1.sprint.orbit import OrbitError, OrbitPolicy, cap_negative_operation_share
from leanfaith.sft1.sprint.runner import canonical_surface, release_certificate_issues
from leanfaith.sft1.sprint.screens import (
    GoldBlocklist,
    render_hash,
    residue_violation,
    unordered_pair_key,
)
from leanfaith.sft1.sprint.square import (
    ENDPOINT_ORIGIN,
    ENDPOINT_ROLE,
    WAVE4_ROW_KINDS,
    ValidatedWave4Root,
    Wave4VariantDescriptor,
    load_wave4_config,
    preselect_wave4_variant_descriptors,
    select_wave4_variants,
    validate_wave4_root_payload,
)
from leanfaith.sft1.sprint.store import Journal, read_json_object, write_atomic


def _mapping_list(value: object, context: str) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list):
        raise CompilerReplayError(f"{context} must be a mapping list")
    return tuple(_mapping(item, f"{context} item") for item in value)


TYPED_CERTIFICATE_GATE_SCHEMA_VERSION = "sft1_wave5_typed_certificate_gate_v1"
TYPED_CERTIFICATE_GATE_RUN_SPEC_VERSION = "sft1_wave5_typed_certificate_gate_run_v1"
TYPED_CERTIFICATE_GATE_CACHE_VERSION = "sft1_wave5_typed_certificate_gate_cache_v1"
TYPED_CERTIFICATE_GATE_REPLAY_VERSION = "sft1_wave5_typed_certificate_gate_replay_v1"
TYPED_CERTIFICATE_GATE_REQUIRED_ROOTS = 1_000
TYPED_CERTIFICATE_GATE_DIRECTORY = "typed_certificate_gate"
TYPED_CERTIFICATE_GATE_TERMINAL = "complete.json"
TYPED_CERTIFICATE_GATE_REPLAY = "forced_resume_replay.json"
_WAVE3_NEW_NEGATIVE_OPERATIONS = frozenset(
    {
        "N26_INCREMENT_BOUND_PROOF_V1",
        "N29_SWAP_WITNESS_DEPENDENCY_PROOF_V1",
        "N30_ADD_UNJUSTIFIED_UNIQUENESS_PROOF_V1",
        "N31_DROP_REQUIRED_GUARD_PROOF_V1",
        "N32_SWAP_ROLE_ORDER_PROOF_V1",
    }
)


@dataclass(frozen=True, slots=True)
class CompilerTypedCertificateRootOutcome:
    """Raw typed evidence collected for one root by the persistent executor."""

    root_id: str
    status: Literal["passed", "failed"]
    taxonomy: str
    descriptor_root: Mapping[str, Any] | None = None
    descriptor_payloads: Mapping[str, Mapping[str, Any]] | None = None
    selected_materializations: tuple[Mapping[str, Any], ...] = ()
    request_hashes: tuple[str, ...] = ()
    raw_response_paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CompilerTypedCertificateExecution:
    """One invocation's evidence plus measured persistent-worker use."""

    outcomes: tuple[CompilerTypedCertificateRootOutcome, ...]
    lean_requests: int
    lean_elapsed_ms: int
    backend_wall_seconds: float
    batch_attempts: int


@dataclass(frozen=True, slots=True)
class CompilerTypedCertificateGateResult:
    run_id: str
    complete_path: Path
    status: Literal["passed", "failed"]
    roots: int
    passed_roots: int
    failed_roots: int
    cache_hits: int
    lean_requests: int


class CompilerTypedCertificateExecutor(Protocol):
    def execute(
        self, sources: Sequence[CompilerAuditSource], *, run_id: str
    ) -> CompilerTypedCertificateExecution: ...

    def close(self) -> None: ...


TypedCertificateExecutorFactory = Callable[[], CompilerTypedCertificateExecutor]


def typed_certificate_gate_root(settings: CompilerAuditSettings) -> Path:
    """Canonical durable root for the proof-certified 1,000-root gate."""

    return settings.output_root / TYPED_CERTIFICATE_GATE_DIRECTORY


def typed_certificate_gate_complete_path(settings: CompilerAuditSettings) -> Path:
    return typed_certificate_gate_root(settings) / TYPED_CERTIFICATE_GATE_TERMINAL


def load_typed_certificate_gate_config(
    path: Path,
) -> tuple[CompilerAuditSettings, CompilerTypedHookSpec, OrbitPolicy]:
    """Load the audit settings, typed operation set, and exact Wave 4 policy."""

    settings = load_compiler_audit_config(path)
    try:
        document = _mapping(yaml.safe_load(path.read_text(encoding="utf-8")), "Wave 5 config")
    except (OSError, yaml.YAMLError) as exc:
        raise CompilerReplayError(f"cannot load typed certificate gate config: {exc}") from exc
    scale = _mapping(document.get("compiler_scale"), "compiler_scale")
    operations = _string_list(scale.get("operations"), "compiler_scale.operations")
    orbit_operations = _string_list(
        scale.get("orbit_operations"), "compiler_scale.orbit_operations"
    )
    spec = CompilerTypedHookSpec(
        operations=operations,
        orbit_operations=orbit_operations,
        maximum_depth=int(scale["maximum_depth"]),
        maximum_variants_per_orbit=int(scale["maximum_variants_per_orbit"]),
        selection_salt=str(scale["typed_selection_salt"]),
    )
    wave4_path = _resolve_path(
        find_repo_root(path), scale.get("wave4_config_path"), "compiler_scale.wave4_config_path"
    )
    policy = load_wave4_config(find_repo_root(wave4_path), wave4_path).policy
    if policy.maximum_depth < spec.maximum_depth:
        raise CompilerReplayError("typed certificate depth exceeds the Wave 4 policy")
    if policy.maximum_variants_per_root < spec.maximum_variants_per_orbit:
        raise CompilerReplayError("typed certificate variant bound exceeds the Wave 4 policy")
    return settings, spec, policy


def _typed_gate_config_semantic_payload(settings: CompilerAuditSettings) -> dict[str, object]:
    return {
        "config_sha256": settings.config_sha256,
        "inventory": {
            "manifest_path": str(settings.inventory_manifest_path),
            "output_root": str(settings.inventory.output_root),
            "source_pin": settings.inventory.pin.to_dict(),
            "project": settings.inventory.project.to_dict(),
            "gold_blocklist_sha256": settings.inventory.gold_blocklist_sha256,
        },
        "audit": {
            "output_root": str(settings.output_root),
            "project_dir": str(settings.project_dir),
            "engine_path": str(settings.engine_path),
            "expected_rows": settings.expected_rows,
            "lean_workers": settings.lean_workers,
            "lean_rss_claim_gib": settings.lean_rss_claim_gib,
            "memory_hard_limit_mb": settings.memory_hard_limit_mb,
            "request_timeout_seconds": settings.request_timeout_seconds,
            "context_request_max_roots": settings.context_request_max_roots,
            "request_batch_size": settings.request_batch_size,
            "retry_max_attempts": settings.retry_max_attempts,
            "retry_statuses": sorted(status.value for status in settings.retry_statuses),
            "elab_async": settings.elab_async,
            "isolate_incremental_commands": settings.isolate_incremental_commands,
            "downstream_mode": settings.downstream_mode,
        },
    }


def _typed_gate_run_identity(
    settings: CompilerAuditSettings,
    spec: CompilerTypedHookSpec,
    policy: OrbitPolicy,
    sample: Sequence[Mapping[str, Any]],
    *,
    required_sample_rows: int,
) -> dict[str, object]:
    repo_root = find_repo_root(settings.engine_path)
    dependencies = {
        "compiler_certificate_gate": hash_file(Path(__file__).resolve()),
        "compiler_replay": hash_file(Path(replay_module.__file__).resolve()),
        "compiler_inventory": hash_file(Path(inventory_module.__file__).resolve()),
        "goal_v1": hash_file(Path(goal_v1_module.__file__).resolve()),
        "orbit": hash_file(Path(orbit_module.__file__).resolve()),
        "runner": hash_file(Path(runner_module.__file__).resolve()),
        "screens": hash_file(Path(screens_module.__file__).resolve()),
        "square": hash_file(Path(square_module.__file__).resolve()),
        "engine": hash_file(settings.engine_path),
    }
    config_payload = _typed_gate_config_semantic_payload(settings)
    spec_payload = spec.semantic_payload()
    return {
        "run_spec_version": TYPED_CERTIFICATE_GATE_RUN_SPEC_VERSION,
        "required_sample_rows": required_sample_rows,
        "audit_sample_path": str(settings.sample_path),
        "audit_sample_sha256": hash_file(settings.sample_path),
        "audit_sample_receipt_sha256": hash_file(settings.sample_receipt_path),
        "sample_rows": len(sample),
        "sample_root_ids_sha256": hash_canonical([str(row["root_id"]) for row in sample]),
        "inventory_manifest_path": str(settings.inventory_manifest_path),
        "inventory_manifest_sha256": hash_file(settings.inventory_manifest_path),
        "inventory_run_spec_sha256": hash_file(
            settings.inventory.output_root / "_state" / "run_spec.json"
        ),
        "audit_config_sha256": settings.config_sha256,
        "audit_config_semantic_hash": hash_canonical(config_payload),
        "audit_config_semantic_payload": config_payload,
        "source_pin": settings.inventory.pin.to_dict(),
        "project": settings.inventory.project.to_dict(),
        "engine_source_sha256": dependencies["engine"],
        "engine_semantic_version": engine_semantic_version(repo_root),
        "typed_spec_hash": hash_canonical(spec_payload),
        "typed_spec": spec_payload,
        "wave4_policy_hash": policy.policy_hash,
        "wave4_policy": policy.payload(),
        "checker": {
            "context_checker_version": CHECKER_VERSION,
            "typed_hook_checker_version": TYPED_HOOK_CHECKER_VERSION,
            "backend_method_version": BACKEND_METHOD_VERSION,
            "goal_v1_renderer_semantic_hash": goal_v1_module.RENDERER_SEMANTIC_HASH,
            "goal_v1_spec_hash": goal_v1_module.SPEC_HASH,
        },
        "checker_hash": hash_canonical(
            {
                "context_checker_version": CHECKER_VERSION,
                "typed_hook_checker_version": TYPED_HOOK_CHECKER_VERSION,
                "backend_method_version": BACKEND_METHOD_VERSION,
                "goal_v1_renderer_semantic_hash": goal_v1_module.RENDERER_SEMANTIC_HASH,
                "goal_v1_spec_hash": goal_v1_module.SPEC_HASH,
                "dependencies": dependencies,
            }
        ),
        "semantic_dependency_sha256": dependencies,
        "execution_contract": {
            "lean_workers_maximum": 2,
            "configured_lean_workers": settings.lean_workers,
            "persistent_project_workers": True,
            "grouping": "byte_identical_context_and_imports",
            "per_root_startup_forbidden": True,
            "retry_only_infrastructure": True,
            "n19_forbidden": True,
            "n25_retained_row_maximum_share": "1/4",
        },
    }


_TYPED_GATE_RELATIONS = "↔≠=≤<≥>∣∈⊆∧∨→"  # noqa: RUF001
_TYPED_GATE_CONNECTIVES = ("¬", "∧", "∨", "→", "↔", "∀", "∃")  # noqa: RUF001


def _typed_gate_pair_delta(reference: str, candidate: str) -> dict[str, Any]:
    def target(text: str) -> str:
        return text.rsplit("⊢", 1)[-1].strip()

    def relation(text: str) -> str:
        value = target(text)
        if value.startswith("¬"):
            return "¬"
        depth = 0
        for character in value:
            if character in "([{":
                depth += 1
            elif character in ")]}":
                depth = max(0, depth - 1)
            elif depth == 0 and character in _TYPED_GATE_RELATIONS:
                return character
        return "none"

    def binder_count(text: str) -> int:
        local_count = sum(
            len(line.partition(" : ")[0].split())
            for line in text.rsplit("⊢", 1)[0].splitlines()
            if " : " in line
        )
        value = target(text)
        return local_count + value.count("∀") + value.count("∃")

    reference_relation = relation(reference)
    candidate_relation = relation(candidate)
    reference_binders = binder_count(reference)
    candidate_binders = binder_count(candidate)
    reference_connectives = tuple(
        target(reference).count(token) for token in _TYPED_GATE_CONNECTIVES
    )
    candidate_connectives = tuple(
        target(candidate).count(token) for token in _TYPED_GATE_CONNECTIVES
    )
    relation_same = reference_relation == candidate_relation
    target_same = target(reference) == target(candidate)
    binders_same = reference_binders == candidate_binders
    connectives_same = reference_connectives == candidate_connectives
    return {
        "cell": "|".join(
            (
                "relation_same" if relation_same else "relation_changed",
                "target_same" if target_same else "target_changed",
                "binders_same" if binders_same else "binders_changed",
                "connectives_same" if connectives_same else "connectives_changed",
            )
        ),
        "relation": {
            "reference": reference_relation,
            "candidate": candidate_relation,
            "agrees": relation_same,
        },
        "target_text_equal": target_same,
        "binders": {
            "reference": reference_binders,
            "candidate": candidate_binders,
            "changed": not binders_same,
        },
        "connectives": {
            "tokens": list(_TYPED_GATE_CONNECTIVES),
            "reference": list(reference_connectives),
            "candidate": list(candidate_connectives),
            "changed": not connectives_same,
        },
    }


def _typed_gate_pair(
    *,
    source: CompilerAuditSource,
    operation_id: str,
    negative_operation: str | None,
    row_kind: str,
    label: bool,
    reference: str,
    candidate: str,
    group_id: str,
    group_size: int,
    preserving_operations: Sequence[str] = (),
) -> dict[str, Any]:
    reference_hash = render_hash(reference)
    candidate_hash = render_hash(candidate)
    identity = {
        "kind": "sft1_wave5_typed_certificate_audit_pair_v1",
        "label": label,
        "unordered_pair_key": unordered_pair_key(reference_hash, candidate_hash),
    }
    pair_delta = _typed_gate_pair_delta(reference, candidate)
    return {
        "row_id": hash_canonical(identity),
        "root_id": source.root_id,
        "operation_id": operation_id,
        "negative_operation": negative_operation,
        "row_kind": row_kind,
        "label": label,
        "reference_render_hash": reference_hash,
        "candidate_render_hash": candidate_hash,
        "unordered_pair_key": unordered_pair_key(reference_hash, candidate_hash),
        "group_id": group_id,
        "group_size": group_size,
        "preserving_operations": list(preserving_operations),
        "pair_delta": pair_delta,
    }


def _typed_gate_screen(goal: str, gold: GoldBlocklist, *, endpoint: str) -> str:
    canonical, violation = canonical_surface(goal)
    if canonical is None:
        raise CompilerReplayError(f"typed gate {endpoint} representation failed: {violation}")
    residue = residue_violation(canonical)
    if residue is not None:
        raise CompilerReplayError(f"typed gate {endpoint} residue screen failed: {residue}")
    if gold.hit(canonical):
        raise CompilerReplayError(f"typed gate {endpoint} hits the gold blocklist")
    return canonical


def _validate_stored_closed_expr_record(
    block: Mapping[str, Any],
    *,
    source: CompilerAuditSource,
    endpoint: str,
    endpoint_id: str,
    expected_goal: str,
    render_scope_id: str,
    compile_context: CompileContext,
) -> None:
    record = _mapping(block.get("record"), "typed gate closed Expr record")
    material = _mapping(block.get("source_material"), "typed gate source material")
    expected_record_fields = {
        "representation_id",
        "goal_v1",
        "goal_v1_source",
        "renderer_version",
        "spec_hash",
        "compile_context_id",
        "endpoint_id",
        "endpoint_role",
        "source_material_hash",
        "rendered_goal_hash",
        "provenance",
        "implementation_identity",
        "typed_alpha_fingerprint",
        "warnings",
    }
    if set(record) != expected_record_fields:
        raise CompilerReplayError("typed gate closed Expr record field set differs")
    expected_material_kind = (
        "raw_statement" if endpoint == "p" else "constructed_expr_no_source_text"
    )
    if material.get("kind") != expected_material_kind:
        raise CompilerReplayError("typed gate closed Expr source-material kind differs")
    if endpoint == "p":
        if material.get("raw_statement") != source.theorem:
            raise CompilerReplayError("typed gate reference source material differs")
    elif (
        not isinstance(material.get("absence_reason"), str)
        or not str(material["absence_reason"]).strip()
    ):
        raise CompilerReplayError("typed gate transformed endpoint lacks an absence reason")
    material_hash = hash_canonical(material)
    expected_fields = {
        "representation_id": str(record.get("representation_id", "")),
        "goal_v1": expected_goal,
        "goal_v1_source": "closed_prop_expr",
        "renderer_version": goal_v1_module.RENDERER_VERSION,
        "spec_hash": goal_v1_module.SPEC_HASH,
        "compile_context_id": compile_context.compile_context_id,
        "endpoint_id": endpoint_id,
        "endpoint_role": ENDPOINT_ROLE[endpoint],
        "source_material_hash": material_hash,
        "rendered_goal_hash": render_hash(expected_goal),
    }
    for field, expected in expected_fields.items():
        if record.get(field) != expected:
            raise CompilerReplayError(f"typed gate closed Expr record differs at {field}")
    provenance = _mapping(record.get("provenance"), "typed gate closed Expr provenance")
    expected_provenance_fields = {
        "expr_hash",
        "expr_hash_algorithm",
        "input_level_params",
        "canonical_level_params",
        "universe_profile_id",
        "universe_profile_hash",
        "render_scope_id",
        "render_context_id",
        "render_context_hash",
        "route_id",
        "expr_origin",
    }
    if set(provenance) != expected_provenance_fields:
        raise CompilerReplayError("typed gate closed Expr provenance field set differs")
    if (
        not isinstance(provenance.get("expr_hash"), str)
        or _SHA256.fullmatch(str(provenance["expr_hash"])) is None
    ):
        raise CompilerReplayError("typed gate closed Expr hash is malformed")
    for field in ("input_level_params", "canonical_level_params"):
        values = provenance.get(field)
        if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
            raise CompilerReplayError(f"typed gate closed Expr {field} is malformed")
    expected_provenance = {
        "expr_hash_algorithm": goal_v1_module.CLOSED_EXPR_HASH_ALGORITHM,
        "render_scope_id": render_scope_id,
        "render_context_id": goal_v1_module.RENDER_CONTEXT_ID,
        "render_context_hash": goal_v1_module.RENDER_CONTEXT_HASH,
        "route_id": goal_v1_module.CLOSED_EXPR_ROUTE_ID,
        "expr_origin": ENDPOINT_ORIGIN[endpoint],
        "universe_profile_id": goal_v1_module.CANONICAL_UNIVERSE_PROFILE_ID,
        "universe_profile_hash": goal_v1_module.CANONICAL_UNIVERSE_PROFILE_HASH,
    }
    for field, expected in expected_provenance.items():
        if provenance.get(field) != expected:
            raise CompilerReplayError(f"typed gate closed Expr provenance differs at {field}")
    implementation = _mapping(
        record.get("implementation_identity"), "typed gate GoalV1 implementation identity"
    )
    if implementation != _implementation_identity().to_dict():
        raise CompilerReplayError("typed gate GoalV1 implementation identity differs")
    identity_payload = {
        "renderer_version": record["renderer_version"],
        "spec_hash": record["spec_hash"],
        "goal_v1_source": record["goal_v1_source"],
        "goal_v1": record["goal_v1"],
        "rendered_goal_hash": record["rendered_goal_hash"],
        "endpoint_id": record["endpoint_id"],
        "endpoint_role": record["endpoint_role"],
        "source_material_hash": record["source_material_hash"],
        "compile_context_id": record["compile_context_id"],
        "provenance": provenance,
        "implementation_identity": implementation,
    }
    if record["representation_id"] != "repr:" + hash_canonical(identity_payload):
        raise CompilerReplayError("typed gate closed Expr representation identity differs")


def _validate_typed_gate_evidence(
    source: CompilerAuditSource,
    outcome: CompilerTypedCertificateRootOutcome,
    *,
    settings: CompilerAuditSettings,
    spec: CompilerTypedHookSpec,
    policy: OrbitPolicy,
    gold: GoldBlocklist,
    engine_version: str,
) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
    if outcome.root_id != source.root_id or outcome.status != "passed":
        raise CompilerReplayError(outcome.taxonomy or "typed gate executor rejected root")
    if outcome.descriptor_root is None or outcome.descriptor_payloads is None:
        raise CompilerReplayError("typed gate root lacks descriptor evidence")
    if not outcome.request_hashes or any(
        _SHA256.fullmatch(value) is None for value in outcome.request_hashes
    ):
        raise CompilerReplayError("typed gate root request hashes are missing or malformed")
    if any(not isinstance(path, str) or not path for path in outcome.raw_response_paths):
        raise CompilerReplayError("typed gate root raw-response paths are malformed")
    root = dict(outcome.descriptor_root)
    binding = _compiler_binding_json(_typed_source_binding(source, settings))
    _require_exact_compiler_binding(root, binding, context="typed gate compiler root")
    _require_checked_source_proof(root)
    if root.get("root") != source.qualified_name or root.get("root_status") != "ok":
        raise CompilerReplayError("typed gate compiler root did not pass")
    if root.get("engine_semantic_version") != engine_version:
        raise CompilerReplayError("typed gate compiler root engine version differs")
    reference_goal = _typed_gate_screen(str(root.get("reference_goal", "")), gold, endpoint="p")
    reference_alpha_hash = root.get("reference_alpha_hash")
    if not isinstance(reference_alpha_hash, str) or not reference_alpha_hash.isdigit():
        raise CompilerReplayError("typed gate compiler root lacks a reference alpha hash")

    terminals = _mapping_list(root.get("terminals"), "typed gate Wave 3 terminals")
    terminal_by_operation: dict[str, dict[str, Any]] = {}
    pairs: list[dict[str, Any]] = []
    retained_by_operation: Counter[str] = Counter()
    for terminal in terminals:
        operation = terminal.get("operation_id")
        if not isinstance(operation, str) or operation in terminal_by_operation:
            raise CompilerReplayError("typed gate Wave 3 terminal operation is missing/duplicate")
        if operation == "N19_WHOLE_CLAIM_NEGATION_V1":
            raise CompilerReplayError("typed gate forbids N19")
        terminal_by_operation[operation] = terminal
        status = terminal.get("status")
        if status not in {"retained", "not_applicable", "rejected"}:
            raise CompilerReplayError(f"typed gate Wave 3 operation {operation} errored")
        if status != "retained":
            continue
        expected_label = operation in POSITIVE_OPERATIONS
        if operation not in POSITIVE_OPERATIONS | NEGATIVE_OPERATIONS:
            raise CompilerReplayError("typed gate retained an unknown operation polarity")
        if terminal.get("label") is not expected_label:
            raise CompilerReplayError("typed gate retained a wrong operation label")
        evidence = _mapping(terminal.get("evidence"), "typed gate Wave 3 evidence")
        certificate_record = {
            "label": expected_label,
            "sidecar": {
                "operation_id": operation,
                "label": expected_label,
                "evidence": evidence,
                "engine": {"semantic_version": engine_version},
            },
        }
        issues = release_certificate_issues(certificate_record)
        if issues:
            raise CompilerReplayError(
                f"typed gate Wave 3 certificate defect for {operation}: {','.join(issues)}"
            )
        candidate = _typed_gate_screen(
            str(terminal.get("candidate_goal", "")), gold, endpoint="candidate"
        )
        candidate_alpha_hash = terminal.get("candidate_alpha_hash")
        if not isinstance(candidate_alpha_hash, str) or not candidate_alpha_hash.isdigit():
            raise CompilerReplayError("typed gate Wave 3 candidate alpha hash is malformed")
        if candidate == reference_goal or candidate_alpha_hash == reference_alpha_hash:
            raise CompilerReplayError("typed gate Wave 3 retained a self-pair")
        group_id = hash_canonical(["sft1_wave5_typed_wave3_group_v1", source.root_id, operation])
        pairs.append(
            _typed_gate_pair(
                source=source,
                operation_id=operation,
                negative_operation=operation if operation in NEGATIVE_OPERATIONS else None,
                row_kind="wave3_operation",
                label=expected_label,
                reference=reference_goal,
                candidate=candidate,
                group_id=group_id,
                group_size=1,
            )
        )
        retained_by_operation[operation] += 1
    if set(terminal_by_operation) != set(spec.operations):
        raise CompilerReplayError("typed gate Wave 3 terminal set differs from the typed spec")

    descriptor_payloads: dict[str, dict[str, Any]] = {}
    for operation, payload in outcome.descriptor_payloads.items():
        if not isinstance(operation, str) or not isinstance(payload, Mapping):
            raise CompilerReplayError("typed gate Wave 4 descriptor mapping is malformed")
        if operation in descriptor_payloads:
            raise CompilerReplayError("typed gate Wave 4 descriptor operation repeats")
        descriptor_payloads[operation] = dict(payload)
    if set(descriptor_payloads) != set(spec.orbit_operations):
        raise CompilerReplayError("typed gate Wave 4 descriptor set differs from the typed spec")
    selected_by_operation: dict[str, dict[str, Any]] = {}
    for raw in outcome.selected_materializations:
        materialization = dict(raw)
        operation = materialization.get("operation_id")
        if not isinstance(operation, str) or operation in selected_by_operation:
            raise CompilerReplayError("typed gate Wave 4 materialization operation repeats")
        selected_by_operation[operation] = materialization

    compile_context, _remainder = _typed_compile_context(source, settings)
    wave4_variants = 0
    for operation, descriptor_payload in descriptor_payloads.items():
        _require_exact_compiler_binding(
            descriptor_payload, binding, context="typed gate Wave 4 descriptor"
        )
        _require_checked_source_proof(descriptor_payload)
        if descriptor_payload.get("source_proof_check") != root.get("source_proof_check"):
            raise CompilerReplayError("typed gate Wave 4 descriptor changes its source proof")
        if descriptor_payload.get("engine_semantic_version") != engine_version:
            raise CompilerReplayError("typed gate Wave 4 descriptor engine version differs")
        descriptor_status = descriptor_payload.get("status")
        if descriptor_status not in {"described", "not_applicable", "rejected"}:
            raise CompilerReplayError("typed gate Wave 4 descriptor has an error status")
        selected_materialization = selected_by_operation.get(operation)
        if descriptor_status != "described":
            if selected_materialization is not None:
                raise CompilerReplayError("typed gate materialized an inapplicable Wave 4 orbit")
            continue
        if selected_materialization is None:
            raise CompilerReplayError("typed gate omitted a described Wave 4 orbit")
        combined = _mapping(
            selected_materialization.get("combined_payload"),
            "typed gate Wave 4 combined payload",
        )
        _require_exact_compiler_binding(
            combined, binding, context="typed gate Wave 4 combined payload"
        )
        _require_checked_source_proof(combined)
        render_scope_id = selected_materialization.get("render_scope_id")
        if not isinstance(render_scope_id, str) or not render_scope_id:
            raise CompilerReplayError("typed gate Wave 4 render scope is missing")
        descriptor_request_hash = selected_materialization.get("descriptor_request_hash")
        selected_request_hash = selected_materialization.get("selected_request_hash")
        if (
            descriptor_request_hash not in outcome.request_hashes
            or selected_request_hash not in outcome.request_hashes
        ):
            raise CompilerReplayError("typed gate Wave 4 request lineage is detached")
        chosen = preselect_wave4_variant_descriptors(
            descriptor_payload,
            operation_id=operation,
            policy=policy,
            maximum_depth=spec.maximum_depth,
            expected_root=source.qualified_name,
            selection_root_id=source.root_id,
        )
        validated = validate_wave4_root_payload(
            combined,
            operation_id=operation,
            policy=policy,
            maximum_depth=spec.maximum_depth,
            expected_root=source.qualified_name,
            selected_descriptors=chosen,
            selection_root_id=source.root_id,
        )
        selected = select_wave4_variants(validated, policy)
        if [variant.index for variant in selected] != [descriptor.index for descriptor in chosen]:
            raise CompilerReplayError("typed gate Wave 4 selection differs from preselection")
        records_value = selected_materialization.get("selected_records")
        if not isinstance(records_value, (list, tuple)):
            raise CompilerReplayError("typed gate Wave 4 selected records are missing")
        records = tuple(
            _mapping(value, "typed gate Wave 4 selected record") for value in records_value
        )
        if len(records) != len(selected):
            raise CompilerReplayError("typed gate Wave 4 selected record count differs")
        for slot, (variant, record) in enumerate(zip(selected, records, strict=True)):
            identity_fields = (
                "index",
                "selection_hash",
                "content_hash",
                "reference_chain_hash",
                "candidate_chain_hash",
                "reference_site_hash",
                "candidate_site_hash",
            )
            for field in identity_fields:
                if record.get(field) != getattr(variant, field):
                    raise CompilerReplayError(
                        f"typed gate Wave 4 selected record differs at {field}"
                    )
            if record.get("variant") != variant.raw:
                raise CompilerReplayError("typed gate Wave 4 selected variant payload differs")
            render = _mapping(record.get("render"), "typed gate Wave 4 render")
            if set(render) != set(_TYPED_ENDPOINTS):
                raise CompilerReplayError("typed gate Wave 4 render endpoint set differs")
            goals = _mapping(variant.raw.get("goals"), "typed gate Wave 4 goals")
            canonical_goals: dict[str, str] = {}
            for endpoint in _TYPED_ENDPOINTS:
                goal = _typed_gate_screen(str(goals.get(endpoint, "")), gold, endpoint=endpoint)
                endpoint_id = typed_wave4_endpoint_id(source.root_id, slot, endpoint)
                _validate_stored_closed_expr_record(
                    _mapping(render[endpoint], "typed gate Wave 4 rendered endpoint"),
                    source=source,
                    endpoint=endpoint,
                    endpoint_id=endpoint_id,
                    expected_goal=goal,
                    render_scope_id=render_scope_id,
                    compile_context=compile_context,
                )
                canonical_goals[endpoint] = goal
            group_id = hash_canonical(
                [
                    "sft1_wave5_typed_wave4_group_v1",
                    source.root_id,
                    operation,
                    variant.selection_hash,
                ]
            )
            preserving_operations = tuple(
                str(hop["p_operation"])
                for hop in _mapping_list(
                    variant.raw.get("hops"), "typed gate Wave 4 preserving hops"
                )
            )
            for (
                row_kind,
                label,
                reference_endpoint,
                candidate_endpoint,
                _evidence_key,
            ) in WAVE4_ROW_KINDS:
                pairs.append(
                    _typed_gate_pair(
                        source=source,
                        operation_id=operation,
                        negative_operation=validated.negative_operation,
                        row_kind=row_kind,
                        label=label,
                        reference=canonical_goals[reference_endpoint],
                        candidate=canonical_goals[candidate_endpoint],
                        group_id=group_id,
                        group_size=len(WAVE4_ROW_KINDS),
                        preserving_operations=preserving_operations,
                    )
                )
            wave4_variants += 1
    if set(selected_by_operation) != {
        operation
        for operation, payload in descriptor_payloads.items()
        if payload.get("status") == "described"
    }:
        raise CompilerReplayError("typed gate Wave 4 materialization set is incomplete")

    if any(pair["reference_render_hash"] == pair["candidate_render_hash"] for pair in pairs):
        raise CompilerReplayError("typed gate retained a self-pair")
    summary = {
        "source_proof_checked": True,
        "wave3_terminal_count": len(terminals),
        "wave3_retained_count": sum(retained_by_operation.values()),
        "wave3_retained_by_operation": dict(sorted(retained_by_operation.items())),
        "wave4_descriptor_count": len(descriptor_payloads),
        "wave4_selected_operations": len(selected_by_operation),
        "wave4_selected_variants": wave4_variants,
        "pair_count": len(pairs),
        "pairs_sha256": hash_canonical(pairs),
    }
    return summary, tuple(pairs)


@dataclass(frozen=True, slots=True)
class _TypedGateSelectedBatch:
    selections: tuple[CompilerTypedWave4Selection, ...]
    descriptor_payloads: Mapping[str, Mapping[str, Any]]
    selected_descriptors: Mapping[str, Sequence[Wave4VariantDescriptor]]
    render_scope_id: str


class _PersistentTypedCertificateExecutor:
    """Two-phase typed gate executor over one persistent central backend."""

    def __init__(
        self,
        settings: CompilerAuditSettings,
        spec: CompilerTypedHookSpec,
        policy: OrbitPolicy,
        *,
        journal: Journal,
        backend_factory: BackendFactory,
    ) -> None:
        self.settings = settings
        self.spec = spec
        self.policy = policy
        self.journal = journal
        backend_settings = _backend_settings(
            replace(settings, output_root=typed_certificate_gate_root(settings))
        )
        self._backend_settings = backend_settings
        self._backend_factory = backend_factory
        self._backend = backend_factory(backend_settings)
        self._closed = False
        self.lean_requests = 0
        self.lean_elapsed_ms = 0
        self.backend_wall_seconds = 0.0
        self.batch_attempts = 0

    def close(self) -> None:
        if not self._closed:
            self._backend.close()
            self._closed = True

    def _reset(self, _attempt: int, _pending: tuple[int, ...]) -> None:
        self._backend.close()
        self._backend = self._backend_factory(self._backend_settings)

    def _run_requests(
        self, requests: Sequence[LeanRequest], *, run_id: str, phase: str
    ) -> tuple[LeanResult, ...]:
        started = time.monotonic()
        outcome = run_batch_with_retries(
            self._backend.run_batch,
            requests,
            RetryPolicy(
                max_attempts=self.settings.retry_max_attempts,
                retry_statuses=self.settings.retry_statuses,
            ),
            before_retry=self._reset,
        )
        wall_seconds = time.monotonic() - started
        request_count = sum(len(lineage) for lineage in outcome.attempts)
        elapsed_ms = sum(result.elapsed_ms for lineage in outcome.attempts for result in lineage)
        self.lean_requests += request_count
        self.lean_elapsed_ms += elapsed_ms
        self.backend_wall_seconds += wall_seconds
        self.batch_attempts += max((len(lineage) for lineage in outcome.attempts), default=0)
        self.journal.append(
            {
                "event": "typed_lean_batch_attempt",
                "run_id": run_id,
                "phase": phase,
                "submitted_requests": len(requests),
                "attempted_requests": request_count,
                "request_ids": [request.request_id for request in requests],
                "attempt_lineage": [
                    [
                        {
                            "request_hash": result.request_hash,
                            "status": result.status.value,
                            "elapsed_ms": result.elapsed_ms,
                            "raw_response_path": result.raw_response_path,
                        }
                        for result in lineage
                    ]
                    for lineage in outcome.attempts
                ],
                "wall_seconds": round(wall_seconds, 6),
            }
        )
        for result in outcome.results:
            if result.status in _NONRESULT_INFRASTRUCTURE:
                raise CompilerReplayInfrastructureError(
                    f"typed certificate gate infrastructure terminal: {result.status.value}"
                )
        return outcome.results

    def _source_batches(
        self, sources: Sequence[CompilerAuditSource]
    ) -> list[tuple[CompilerAuditSource, ...]]:
        grouped: dict[tuple[str, str], list[CompilerAuditSource]] = defaultdict(list)
        for source in sources:
            grouped[
                (
                    source.context_fingerprint,
                    sha256_hex(source.context_prefix.encode("utf-8")),
                )
            ].append(source)
        batches: list[tuple[CompilerAuditSource, ...]] = []
        for key in sorted(grouped):
            current: list[CompilerAuditSource] = []
            names: set[str | None] = set()
            for source in sorted(grouped[key], key=lambda item: item.root_id):
                if (
                    len(current) >= self.settings.context_request_max_roots
                    or source.qualified_name in names
                ):
                    batches.append(tuple(current))
                    current = []
                    names = set()
                current.append(source)
                names.add(source.qualified_name)
            if current:
                batches.append(tuple(current))
        return batches

    @staticmethod
    def _bisect_sources(
        sources: tuple[CompilerAuditSource, ...],
    ) -> tuple[tuple[CompilerAuditSource, ...], ...]:
        if len(sources) <= 1:
            return (sources,)
        middle = len(sources) // 2
        return sources[:middle], sources[middle:]

    def execute(
        self, sources: Sequence[CompilerAuditSource], *, run_id: str
    ) -> CompilerTypedCertificateExecution:
        context_id = "ctx:" + _backend_context_fingerprint(self.settings)
        states: dict[str, dict[str, Any]] = {
            source.root_id: {
                "source": source,
                "status": "pending",
                "taxonomy": "",
                "descriptor_root": None,
                "descriptor_payloads": None,
                "selected": [],
                "request_hashes": [],
                "raw_response_paths": [],
            }
            for source in sources
        }
        descriptor_queue = self._source_batches(sources)
        while descriptor_queue:
            groups = descriptor_queue[: self.settings.request_batch_size]
            del descriptor_queue[: self.settings.request_batch_size]
            prepared = [
                build_typed_descriptor_batch_request(
                    group,
                    settings=self.settings,
                    spec=self.spec,
                    context_id=context_id,
                    timeout_seconds=self.settings.request_timeout_seconds,
                    run_id=run_id,
                )
                for group in groups
            ]
            results = self._run_requests(
                [item.request for item in prepared], run_id=run_id, phase="descriptor"
            )
            for group, _request, result in zip(groups, prepared, results, strict=True):
                parse_error: str | None = None
                parsed: dict[str, tuple[dict[str, Any], dict[str, dict[str, Any]]]] | None = None
                if (
                    result.status == LeanStatus.VALID
                    and not result.sorries
                    and not _messages_report_sorry(result.messages)
                ):
                    try:
                        parsed = parse_typed_descriptor_batch_payloads(
                            group,
                            settings=self.settings,
                            spec=self.spec,
                            messages=result.messages,
                        )
                    except (CompilerReplayError, OrbitError, ValueError) as exc:
                        parse_error = f"descriptor_evidence:{str(exc)[:400]}"
                else:
                    parse_error = f"descriptor_lean_status:{result.status.value}"
                if parse_error is not None or parsed is None:
                    if len(group) > 1:
                        descriptor_queue[0:0] = list(self._bisect_sources(group))
                        continue
                    state = states[group[0].root_id]
                    state["status"] = "failed"
                    state["taxonomy"] = parse_error or "descriptor_evidence_missing"
                    state["request_hashes"].append(result.request_hash)
                    if result.raw_response_path:
                        state["raw_response_paths"].append(result.raw_response_path)
                    continue
                for source in group:
                    root, descriptors = parsed[source.root_id]
                    state = states[source.root_id]
                    state["request_hashes"].append(result.request_hash)
                    if result.raw_response_path:
                        state["raw_response_paths"].append(result.raw_response_path)
                    if root.get("root_status") != "ok":
                        state["status"] = "failed"
                        state["taxonomy"] = (
                            "compiler_root:"
                            + str(root.get("reason", root.get("root_status")))[:300]
                        )
                        state["descriptor_root"] = root
                        state["descriptor_payloads"] = descriptors
                        continue
                    state["status"] = "passed"
                    state["descriptor_root"] = root
                    state["descriptor_payloads"] = descriptors

        selected_items: list[
            tuple[
                CompilerTypedWave4Selection,
                Mapping[str, Any],
                tuple[Wave4VariantDescriptor, ...],
            ]
        ] = []
        for source in sources:
            state = states[source.root_id]
            if state["status"] != "passed":
                continue
            descriptor_map = cast(Mapping[str, Mapping[str, Any]], state["descriptor_payloads"])
            for operation in self.spec.orbit_operations:
                payload = descriptor_map[operation]
                if payload.get("status") != "described":
                    continue
                try:
                    chosen = preselect_wave4_variant_descriptors(
                        payload,
                        operation_id=operation,
                        policy=self.policy,
                        maximum_depth=self.spec.maximum_depth,
                        expected_root=source.qualified_name,
                        selection_root_id=source.root_id,
                    )
                except (OrbitError, ValueError) as exc:
                    state["status"] = "failed"
                    state["taxonomy"] = f"wave4_descriptor:{str(exc)[:400]}"
                    break
                if not chosen:
                    state["status"] = "failed"
                    state["taxonomy"] = "wave4_descriptor:selected_empty"
                    break
                selected_items.append(
                    (
                        CompilerTypedWave4Selection(
                            source=source,
                            operation_id=operation,
                            selected_indices=tuple(item.index for item in chosen),
                        ),
                        payload,
                        chosen,
                    )
                )

        grouped_selected: dict[
            tuple[str, str, str],
            list[
                tuple[
                    CompilerTypedWave4Selection,
                    Mapping[str, Any],
                    tuple[Wave4VariantDescriptor, ...],
                ]
            ],
        ] = defaultdict(list)
        for item in selected_items:
            selection = item[0]
            if states[selection.source.root_id]["status"] != "passed":
                continue
            grouped_selected[
                (
                    selection.operation_id,
                    selection.source.context_fingerprint,
                    sha256_hex(selection.source.context_prefix.encode("utf-8")),
                )
            ].append(item)
        selected_queue: list[_TypedGateSelectedBatch] = []
        for key in sorted(grouped_selected):
            ordered = sorted(grouped_selected[key], key=lambda item: item[0].source.root_id)
            for offset in range(0, len(ordered), self.settings.context_request_max_roots):
                chunk = ordered[offset : offset + self.settings.context_request_max_roots]
                selections = tuple(item[0] for item in chunk)
                root_ids = [selection.source.root_id for selection in selections]
                scope = "sft1-wave5-typed-certificate-gate:" + hash_canonical(
                    [run_id, key[0], root_ids]
                )
                selected_queue.append(
                    _TypedGateSelectedBatch(
                        selections=selections,
                        descriptor_payloads={item[0].source.root_id: item[1] for item in chunk},
                        selected_descriptors={item[0].source.root_id: item[2] for item in chunk},
                        render_scope_id=scope,
                    )
                )

        while selected_queue:
            works = selected_queue[: self.settings.request_batch_size]
            del selected_queue[: self.settings.request_batch_size]
            prepared = [
                build_typed_wave4_selected_batch_request(
                    work.selections,
                    settings=self.settings,
                    spec=self.spec,
                    render_scope_id=work.render_scope_id,
                    context_id=context_id,
                    timeout_seconds=self.settings.request_timeout_seconds,
                    run_id=run_id,
                )
                for work in works
            ]
            results = self._run_requests(
                [item.request for item in prepared], run_id=run_id, phase="wave4_selected"
            )
            for work, _request, result in zip(works, prepared, results, strict=True):
                validation_error: str | None = None
                materialized: (
                    dict[
                        str,
                        tuple[dict[str, Any], ValidatedWave4Root, tuple[dict[str, Any], ...]],
                    ]
                    | None
                ) = None
                if (
                    result.status == LeanStatus.VALID
                    and not result.sorries
                    and not _messages_report_sorry(result.messages)
                ):
                    try:
                        materialized = validate_typed_wave4_selected_batch_result(
                            work.selections,
                            settings=self.settings,
                            spec=self.spec,
                            descriptor_payloads=work.descriptor_payloads,
                            selected_descriptors=work.selected_descriptors,
                            render_scope_id=work.render_scope_id,
                            policy=self.policy,
                            result=result,
                        )
                    except (CompilerReplayError, GoalV1Error, OrbitError, ValueError) as exc:
                        validation_error = f"wave4_selected_evidence:{str(exc)[:400]}"
                else:
                    validation_error = f"wave4_selected_lean_status:{result.status.value}"
                if validation_error is not None or materialized is None:
                    if len(work.selections) > 1:
                        middle = len(work.selections) // 2
                        for selected_slice in (
                            work.selections[:middle],
                            work.selections[middle:],
                        ):
                            ids = [selection.source.root_id for selection in selected_slice]
                            scope = "sft1-wave5-typed-certificate-gate:" + hash_canonical(
                                [run_id, selected_slice[0].operation_id, ids]
                            )
                            selected_queue.insert(
                                0,
                                _TypedGateSelectedBatch(
                                    selections=selected_slice,
                                    descriptor_payloads={
                                        root_id: work.descriptor_payloads[root_id]
                                        for root_id in ids
                                    },
                                    selected_descriptors={
                                        root_id: work.selected_descriptors[root_id]
                                        for root_id in ids
                                    },
                                    render_scope_id=scope,
                                ),
                            )
                        continue
                    source = work.selections[0].source
                    state = states[source.root_id]
                    state["status"] = "failed"
                    state["taxonomy"] = validation_error or "wave4_selected_evidence_missing"
                    state["request_hashes"].append(result.request_hash)
                    if result.raw_response_path:
                        state["raw_response_paths"].append(result.raw_response_path)
                    continue
                for selection in work.selections:
                    source = selection.source
                    root_id = source.root_id
                    combined, _validated, selected_records = materialized[root_id]
                    state = states[root_id]
                    state["request_hashes"].append(result.request_hash)
                    if result.raw_response_path:
                        state["raw_response_paths"].append(result.raw_response_path)
                    state["selected"].append(
                        {
                            "operation_id": selection.operation_id,
                            "render_scope_id": work.render_scope_id,
                            "combined_payload": combined,
                            "selected_records": list(selected_records),
                            "descriptor_request_hash": state["request_hashes"][0],
                            "selected_request_hash": result.request_hash,
                            "raw_response_path": result.raw_response_path,
                        }
                    )

        outcomes = tuple(
            CompilerTypedCertificateRootOutcome(
                root_id=source.root_id,
                status=cast(Literal["passed", "failed"], states[source.root_id]["status"]),
                taxonomy=str(states[source.root_id]["taxonomy"] or "typed_certificates_checked"),
                descriptor_root=states[source.root_id]["descriptor_root"],
                descriptor_payloads=states[source.root_id]["descriptor_payloads"],
                selected_materializations=tuple(states[source.root_id]["selected"]),
                request_hashes=tuple(states[source.root_id]["request_hashes"]),
                raw_response_paths=tuple(states[source.root_id]["raw_response_paths"]),
            )
            for source in sources
        )
        return CompilerTypedCertificateExecution(
            outcomes=outcomes,
            lean_requests=self.lean_requests,
            lean_elapsed_ms=self.lean_elapsed_ms,
            backend_wall_seconds=round(self.backend_wall_seconds, 6),
            batch_attempts=self.batch_attempts,
        )


def _typed_gate_cache_key_payload(
    source: CompilerAuditSource,
    *,
    run_id: str,
    settings: CompilerAuditSettings,
    spec: CompilerTypedHookSpec,
    policy: OrbitPolicy,
) -> dict[str, object]:
    source_locator = _mapping(source.inventory_record["source"], "inventory source")
    source_hashes = _mapping(source.inventory_record["hashes"], "inventory hashes")
    context = _mapping(source.inventory_record["context"], "inventory context")
    declaration = _mapping(source.inventory_record["declaration"], "inventory declaration")
    return {
        "cache_version": TYPED_CERTIFICATE_GATE_CACHE_VERSION,
        "run_id": run_id,
        "root_id": source.root_id,
        "inventory_record_sha256": source.inventory_record_sha256,
        "source_row_id": source.inventory_record["source_row_id"],
        "source_locator": dict(source_locator),
        "source_hashes": dict(source_hashes),
        "context": {
            "context_sha256": context["context_sha256"],
            "context_fingerprint": context["context_fingerprint"],
        },
        "qualified_name": declaration.get("qualified_name_candidate"),
        "source_pin": settings.inventory.pin.to_dict(),
        "project": settings.inventory.project.to_dict(),
        "typed_spec_hash": hash_canonical(spec.semantic_payload()),
        "wave4_policy_hash": policy.policy_hash,
    }


def _typed_gate_cache_path(settings: CompilerAuditSettings, cache_key: str) -> Path:
    return typed_certificate_gate_root(settings) / "cache" / cache_key[:2] / f"{cache_key}.json"


def _typed_gate_outcome_evidence(
    outcome: CompilerTypedCertificateRootOutcome,
) -> dict[str, object]:
    return {
        "executor_status": outcome.status,
        "executor_taxonomy": outcome.taxonomy,
        "descriptor_root": (
            dict(outcome.descriptor_root) if outcome.descriptor_root is not None else None
        ),
        "descriptor_payloads": (
            {operation: dict(payload) for operation, payload in outcome.descriptor_payloads.items()}
            if outcome.descriptor_payloads is not None
            else None
        ),
        "selected_materializations": [
            dict(materialization) for materialization in outcome.selected_materializations
        ],
        "request_hashes": list(outcome.request_hashes),
        "raw_response_paths": list(outcome.raw_response_paths),
    }


def _typed_gate_terminal_record(
    source: CompilerAuditSource,
    *,
    run_id: str,
    settings: CompilerAuditSettings,
    spec: CompilerTypedHookSpec,
    policy: OrbitPolicy,
    status: Literal["passed", "failed"],
    taxonomy: str,
    outcome: CompilerTypedCertificateRootOutcome | None,
    validation_summary: Mapping[str, Any] | None,
    pairs: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    key_payload = _typed_gate_cache_key_payload(
        source,
        run_id=run_id,
        settings=settings,
        spec=spec,
        policy=policy,
    )
    cache_key = hash_canonical(key_payload)
    record: dict[str, Any] = {
        "artifact_kind": "sft1_wave5_typed_certificate_root_terminal",
        "schema_version": TYPED_CERTIFICATE_GATE_CACHE_VERSION,
        "cache_key": cache_key,
        "key_payload": key_payload,
        "root_id": source.root_id,
        "status": status,
        "taxonomy": taxonomy[:1000],
        "validation_summary": (
            dict(validation_summary) if validation_summary is not None else None
        ),
        "pairs": [dict(pair) for pair in pairs],
        "evidence": _typed_gate_outcome_evidence(outcome) if outcome is not None else None,
    }
    record["cache_record_sha256"] = hash_canonical(record)
    return record


def _load_typed_gate_cache(
    source: CompilerAuditSource,
    *,
    run_id: str,
    settings: CompilerAuditSettings,
    spec: CompilerTypedHookSpec,
    policy: OrbitPolicy,
) -> tuple[dict[str, Any], Path] | None:
    key_payload = _typed_gate_cache_key_payload(
        source,
        run_id=run_id,
        settings=settings,
        spec=spec,
        policy=policy,
    )
    cache_key = hash_canonical(key_payload)
    path = _typed_gate_cache_path(settings, cache_key)
    if not path.is_file():
        return None
    if path.is_symlink():
        raise CompilerReplayError(f"unsafe typed certificate cache symlink: {path}")
    try:
        record = read_json_object(path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise CompilerReplayError(f"cannot read typed certificate cache {path}: {exc}") from exc
    digest = record.pop("cache_record_sha256", None)
    if digest != hash_canonical(record):
        raise CompilerReplayError(f"typed certificate cache content hash differs: {path}")
    record["cache_record_sha256"] = digest
    required = {
        "artifact_kind": "sft1_wave5_typed_certificate_root_terminal",
        "schema_version": TYPED_CERTIFICATE_GATE_CACHE_VERSION,
        "cache_key": cache_key,
        "key_payload": key_payload,
        "root_id": source.root_id,
    }
    for field, expected in required.items():
        if record.get(field) != expected:
            raise CompilerReplayError(f"typed certificate cache differs at {field}: {path}")
    if record.get("status") not in {"passed", "failed"}:
        raise CompilerReplayError(f"typed certificate cache status is nonterminal: {path}")
    taxonomy = record.get("taxonomy")
    if not isinstance(taxonomy, str) or not taxonomy:
        raise CompilerReplayError(f"typed certificate cache taxonomy is missing: {path}")
    return record, path


def _typed_gate_outcome_from_record(
    source: CompilerAuditSource, record: Mapping[str, Any]
) -> CompilerTypedCertificateRootOutcome:
    evidence = _mapping(record.get("evidence"), "typed certificate cached evidence")
    descriptor_root_value = evidence.get("descriptor_root")
    descriptor_root = (
        _mapping(descriptor_root_value, "typed certificate descriptor root")
        if descriptor_root_value is not None
        else None
    )
    descriptor_payloads_value = evidence.get("descriptor_payloads")
    descriptor_payloads: dict[str, dict[str, Any]] | None = None
    if descriptor_payloads_value is not None:
        descriptor_mapping = _mapping(
            descriptor_payloads_value, "typed certificate descriptor payloads"
        )
        descriptor_payloads = {}
        for operation, payload in descriptor_mapping.items():
            if not isinstance(operation, str):
                raise CompilerReplayError("typed certificate descriptor key is not text")
            descriptor_payloads[operation] = _mapping(
                payload, "typed certificate descriptor payload"
            )
    selected = _mapping_list(
        evidence.get("selected_materializations"),
        "typed certificate selected materializations",
    )
    status = evidence.get("executor_status")
    if status not in {"passed", "failed"}:
        raise CompilerReplayError("typed certificate cached executor status is invalid")
    taxonomy = evidence.get("executor_taxonomy")
    if not isinstance(taxonomy, str) or not taxonomy:
        raise CompilerReplayError("typed certificate cached executor taxonomy is missing")
    return CompilerTypedCertificateRootOutcome(
        root_id=source.root_id,
        status=cast(Literal["passed", "failed"], status),
        taxonomy=taxonomy,
        descriptor_root=descriptor_root,
        descriptor_payloads=descriptor_payloads,
        selected_materializations=selected,
        request_hashes=_string_list(
            evidence.get("request_hashes"), "typed certificate request hashes"
        ),
        raw_response_paths=_string_list(
            evidence.get("raw_response_paths"), "typed certificate raw-response paths"
        ),
    )


@dataclass(frozen=True, slots=True)
class _TypedGateGroup:
    group_id: str
    operation_id: str
    mechanism: str
    row_ids: tuple[str, ...]
    pairs: tuple[Mapping[str, Any], ...]


def _validate_typed_gate_cache_record(
    source: CompilerAuditSource,
    record: Mapping[str, Any],
    *,
    settings: CompilerAuditSettings,
    spec: CompilerTypedHookSpec,
    policy: OrbitPolicy,
    gold: GoldBlocklist,
    engine_version: str,
) -> None:
    status = record.get("status")
    pairs = _mapping_list(record.get("pairs"), "typed certificate cached pairs")
    validation_summary_value = record.get("validation_summary")
    evidence = record.get("evidence")
    if status == "failed":
        if pairs or validation_summary_value is not None:
            raise CompilerReplayError("failed typed certificate cache retains validated pairs")
        if evidence is not None:
            _typed_gate_outcome_from_record(source, record)
        return
    if status != "passed" or evidence is None:
        raise CompilerReplayError("passed typed certificate cache lacks executor evidence")
    outcome = _typed_gate_outcome_from_record(source, record)
    summary, replayed_pairs = _validate_typed_gate_evidence(
        source,
        outcome,
        settings=settings,
        spec=spec,
        policy=policy,
        gold=gold,
        engine_version=engine_version,
    )
    validation_summary = _mapping(validation_summary_value, "typed certificate validation summary")
    if validation_summary != summary or pairs != replayed_pairs:
        raise CompilerReplayError("typed certificate cached validation replay differs")


def _typed_gate_yield_table(
    pairs: Sequence[Mapping[str, Any]], retained_ids: set[str]
) -> dict[str, dict[str, int]]:
    table: dict[str, Counter[str]] = defaultdict(Counter)
    seen_groups: dict[str, set[str]] = defaultdict(set)
    retained_groups: dict[str, set[str]] = defaultdict(set)
    roots: dict[str, set[str]] = defaultdict(set)
    retained_roots: dict[str, set[str]] = defaultdict(set)
    for pair in pairs:
        operation = str(pair["operation_id"])
        row_id = str(pair["row_id"])
        group_id = str(pair["group_id"])
        root_id = str(pair["root_id"])
        table[operation]["emitted_rows"] += 1
        seen_groups[operation].add(group_id)
        roots[operation].add(root_id)
        if row_id in retained_ids:
            table[operation]["retained_rows"] += 1
            retained_groups[operation].add(group_id)
            retained_roots[operation].add(root_id)
    return {
        operation: {
            "emitted_rows": counts["emitted_rows"],
            "retained_rows": counts["retained_rows"],
            "emitted_groups": len(seen_groups[operation]),
            "retained_groups": len(retained_groups[operation]),
            "emitting_roots": len(roots[operation]),
            "retained_roots": len(retained_roots[operation]),
        }
        for operation, counts in sorted(table.items())
    }


def _typed_gate_integrity(
    sources: Sequence[CompilerAuditSource],
    records: Sequence[Mapping[str, Any]],
    *,
    spec: CompilerTypedHookSpec,
    policy: OrbitPolicy,
) -> dict[str, Any]:
    source_by_root = {source.root_id: source for source in sources}
    pairs: list[dict[str, Any]] = []
    passed_roots: set[str] = set()
    failed_roots: set[str] = set()
    for record in records:
        root_id = str(record["root_id"])
        if root_id not in source_by_root:
            raise CompilerReplayError("typed certificate cache names a root outside the sample")
        if record["status"] == "passed":
            passed_roots.add(root_id)
        else:
            failed_roots.add(root_id)
        for pair in _mapping_list(record.get("pairs"), "typed certificate root pairs"):
            if pair.get("root_id") != root_id:
                raise CompilerReplayError("typed certificate pair changes its ancestry root")
            pairs.append(pair)
    if passed_roots & failed_roots or len(passed_roots | failed_roots) != len(sources):
        raise CompilerReplayError("typed certificate root terminal coverage differs")

    physical_by_id: dict[str, tuple[str, str, bool, str, str]] = {}
    stable_id_groups: dict[str, list[str]] = defaultdict(list)
    same_label_pair_class_groups: dict[tuple[str, bool], list[str]] = defaultdict(list)
    seen_group_rows: set[tuple[str, str]] = set()
    unordered: dict[str, set[bool]] = defaultdict(set)
    group_pairs: dict[str, list[dict[str, Any]]] = defaultdict(list)
    wrong_labels = 0
    self_pairs = 0
    n19_rows = 0
    structurally_validated_pairs = 0
    pair_delta_cells: Counter[str] = Counter()
    preserving_rows: Counter[str] = Counter()
    wave4_labels = {
        row_kind: label for row_kind, label, _reference, _candidate, _evidence in WAVE4_ROW_KINDS
    }
    for pair in pairs:
        row_id = pair.get("row_id")
        reference_hash = pair.get("reference_render_hash")
        candidate_hash = pair.get("candidate_render_hash")
        unordered_key = pair.get("unordered_pair_key")
        if not isinstance(row_id, str) or _SHA256.fullmatch(row_id) is None:
            raise CompilerReplayError("typed certificate pair row ID is malformed")
        if not isinstance(reference_hash, str) or _SHA256.fullmatch(reference_hash) is None:
            raise CompilerReplayError("typed certificate reference render hash is malformed")
        if not isinstance(candidate_hash, str) or _SHA256.fullmatch(candidate_hash) is None:
            raise CompilerReplayError("typed certificate candidate render hash is malformed")
        if unordered_key != unordered_pair_key(reference_hash, candidate_hash):
            raise CompilerReplayError("typed certificate unordered pair key differs")
        label = pair.get("label")
        if type(label) is not bool:
            raise CompilerReplayError("typed certificate pair label is not Boolean")
        operation = pair.get("operation_id")
        row_kind = pair.get("row_kind")
        if not isinstance(operation, str) or not isinstance(row_kind, str):
            raise CompilerReplayError("typed certificate pair operation identity is malformed")
        group_id = pair.get("group_id")
        if not isinstance(group_id, str) or _SHA256.fullmatch(group_id) is None:
            raise CompilerReplayError("typed certificate pair group ID is malformed")
        negative = pair.get("negative_operation")
        if negative == "N19_WHOLE_CLAIM_NEGATION_V1":
            n19_rows += 1
        if row_kind == "wave3_operation":
            expected_label = operation in POSITIVE_OPERATIONS
            if operation not in spec.operations:
                raise CompilerReplayError("typed certificate Wave 3 pair is outside the spec")
        else:
            wave4_expected_label = wave4_labels.get(row_kind)
            if operation not in spec.orbit_operations or wave4_expected_label is None:
                raise CompilerReplayError("typed certificate Wave 4 pair is outside the spec")
            expected_label = wave4_expected_label
        if label is not expected_label:
            wrong_labels += 1
        identity = {
            "kind": "sft1_wave5_typed_certificate_audit_pair_v1",
            "label": label,
            "unordered_pair_key": unordered_key,
        }
        if row_id != hash_canonical(identity):
            raise CompilerReplayError("typed certificate pair stable row ID differs")
        if reference_hash == candidate_hash:
            self_pairs += 1
        pair_delta = _mapping(pair.get("pair_delta"), "typed certificate pair delta")
        cell = pair_delta.get("cell")
        if not isinstance(cell, str) or not cell:
            raise CompilerReplayError("typed certificate pair delta cell is missing")
        physical_identity = (
            reference_hash,
            candidate_hash,
            label,
            str(unordered_key),
            hash_canonical(pair_delta),
        )
        prior_physical = physical_by_id.get(row_id)
        if prior_physical is not None and prior_physical != physical_identity:
            raise CompilerReplayError(
                "typed certificate shared physical row has identity/content drift"
            )
        group_row = (group_id, row_id)
        if group_row in seen_group_rows:
            raise CompilerReplayError("typed certificate repeats a physical row within a group")
        seen_group_rows.add(group_row)
        physical_by_id.setdefault(row_id, physical_identity)
        stable_id_groups[row_id].append(group_id)
        same_label_pair_class_groups[(str(unordered_key), label)].append(group_id)
        unordered[str(unordered_key)].add(label)
        group_pairs[group_id].append(pair)
        pair_delta_cells[cell] += 1
        preserving_operations = pair.get("preserving_operations")
        if not isinstance(preserving_operations, list) or not all(
            isinstance(item, str) for item in preserving_operations
        ):
            raise CompilerReplayError("typed certificate preserving operation list is malformed")
        for preserving in cast(list[str], preserving_operations):
            if preserving not in policy.operation_map():
                raise CompilerReplayError("typed certificate uses an unregistered preserving op")
            preserving_rows[preserving] += 1
        structurally_validated_pairs += 1

    duplicate_stable_ids = sum(
        len(group_ids) - 1 for group_ids in stable_id_groups.values() if len(group_ids) > 1
    )
    duplicate_stable_id_classes = sum(len(group_ids) > 1 for group_ids in stable_id_groups.values())
    duplicate_pair_classes = sum(
        len(group_ids) - 1
        for group_ids in same_label_pair_class_groups.values()
        if len(group_ids) > 1
    )
    duplicate_same_label_pair_classes = sum(
        len(group_ids) > 1 for group_ids in same_label_pair_class_groups.values()
    )
    conflicting_pair_classes = sum(len(labels) > 1 for labels in unordered.values())
    partial_groups = 0
    complete_group_ids: set[str] = set()
    complete_wave4_closure_groups = 0
    groups: list[_TypedGateGroup] = []
    for group_id, group in sorted(group_pairs.items()):
        expected_sizes = {pair.get("group_size") for pair in group}
        operations = {str(pair.get("operation_id")) for pair in group}
        negatives = {pair.get("negative_operation") for pair in group}
        if (
            len(expected_sizes) != 1
            or len(operations) != 1
            or len(negatives) != 1
            or type(next(iter(expected_sizes))) is not int
            or len(group) != next(iter(expected_sizes))
        ):
            partial_groups += 1
            continue
        operation = next(iter(operations))
        row_kinds = Counter(str(pair.get("row_kind")) for pair in group)
        if operation in spec.operations:
            complete_shape = len(group) == 1 and row_kinds == Counter({"wave3_operation": 1})
        else:
            complete_shape = (
                operation in spec.orbit_operations
                and len(group) == len(WAVE4_ROW_KINDS)
                and row_kinds == Counter(row_kind for row_kind, *_rest in WAVE4_ROW_KINDS)
            )
        if not complete_shape:
            partial_groups += 1
            continue
        complete_group_ids.add(group_id)
        if operation in spec.orbit_operations:
            complete_wave4_closure_groups += 1
        negative = next(iter(negatives))
        operation_id = str(negative) if isinstance(negative, str) else "NON_NEGATIVE"
        mechanism = mechanism_of(operation_id) if operation_id in NEGATIVE_OPERATIONS else "none"
        groups.append(
            _TypedGateGroup(
                group_id=group_id,
                operation_id=operation_id,
                mechanism=mechanism,
                row_ids=tuple(str(pair["row_id"]) for pair in group),
                pairs=tuple(group),
            )
        )
    modeled_stable_id_references = sum(
        len(group_ids) - 1
        for group_ids in stable_id_groups.values()
        if len(group_ids) > 1
        and len(group_ids) == len(set(group_ids))
        and set(group_ids) <= complete_group_ids
    )
    modeled_pair_class_references = sum(
        len(group_ids) - 1
        for group_ids in same_label_pair_class_groups.values()
        if len(group_ids) > 1
        and len(group_ids) == len(set(group_ids))
        and set(group_ids) <= complete_group_ids
    )
    unmodeled_duplicate_stable_ids = duplicate_stable_ids - modeled_stable_id_references
    unmodeled_duplicate_pair_classes = duplicate_pair_classes - modeled_pair_class_references
    cap = cap_negative_operation_share(
        groups,
        "N25_TOGGLE_EQ_NE_PROOF_V1",
        0.25,
        selection_salt=f"{spec.selection_salt}:typed-certificate-gate:n25",
    )
    selected_group_ids = {group.group_id for group in cap.selected_groups}
    retained_logical_pairs = [pair for pair in pairs if str(pair["group_id"]) in selected_group_ids]
    retained_pair_by_id: dict[str, dict[str, Any]] = {}
    for pair in retained_logical_pairs:
        retained_pair_by_id.setdefault(str(pair["row_id"]), pair)
    retained_ids = set(retained_pair_by_id)
    n25_ids = {
        str(pair["row_id"])
        for pair in retained_logical_pairs
        if pair.get("negative_operation") == "N25_TOGGLE_EQ_NE_PROOF_V1"
    }
    n25_rows = len(n25_ids)
    n25_share = n25_rows / len(retained_ids) if retained_ids else 0.0
    retained_positive_rows = sum(pair["label"] is True for pair in retained_pair_by_id.values())
    retained_negative_rows = sum(pair["label"] is False for pair in retained_pair_by_id.values())
    requested_positive_operations = sorted(set(spec.operations) & POSITIVE_OPERATIONS)
    requested_negative_operations = sorted(set(spec.operations) & NEGATIVE_OPERATIONS)
    requested_new_negative_families = sorted(set(spec.operations) & _WAVE3_NEW_NEGATIVE_OPERATIONS)
    useful_new_negative_families = sorted(
        {
            str(pair["negative_operation"])
            for pair in retained_logical_pairs
            if pair.get("row_kind") == "wave3_operation"
            and pair.get("negative_operation") in requested_new_negative_families
        }
    )
    required_useful_new_negative_families = min(3, len(requested_new_negative_families))

    source_yields: dict[str, dict[str, int]] = {}
    for split in sorted({source.shard.split for source in sources}):
        split_roots = {source.root_id for source in sources if source.shard.split == split}
        emitted_ids = {str(pair["row_id"]) for pair in pairs if pair["root_id"] in split_roots}
        retained = {
            str(pair["row_id"]) for pair in retained_logical_pairs if pair["root_id"] in split_roots
        }
        source_yields[split] = {
            "sample_roots": len(split_roots),
            "passed_roots": len(split_roots & passed_roots),
            "failed_roots": len(split_roots & failed_roots),
            "emitted_rows": len(emitted_ids),
            "retained_rows": len(retained),
        }
    negative_yields: dict[str, dict[str, int]] = {}
    for operation in sorted(
        {
            str(pair["negative_operation"])
            for pair in pairs
            if isinstance(pair.get("negative_operation"), str)
        }
    ):
        emitted = [pair for pair in pairs if pair.get("negative_operation") == operation]
        retained_negative = [
            pair for pair in retained_logical_pairs if pair.get("negative_operation") == operation
        ]
        negative_yields[operation] = {
            "emitted_rows": len(emitted),
            "retained_rows": len(retained_negative),
            "emitting_roots": len({str(pair["root_id"]) for pair in emitted}),
            "retained_roots": len({str(pair["root_id"]) for pair in retained_negative}),
        }

    checks = {
        "all_sample_roots_terminal": len(passed_roots | failed_roots) == len(sources),
        "all_source_proofs_checked": len(passed_roots) == len(sources),
        "all_wave3_certificates_exact": len(passed_roots) == len(sources),
        "all_wave4_closures_exact": len(passed_roots) == len(sources),
        "candidate_and_reference_screens_passed": len(passed_roots) == len(sources),
        "all_emitted_pairs_automatically_structurally_validated": (
            structurally_validated_pairs == len(pairs)
        ),
        "zero_wrong_labels": wrong_labels == 0,
        "zero_self_pairs": self_pairs == 0,
        "duplicate_stable_ids_only_model_cross_group_shared_rows": (
            unmodeled_duplicate_stable_ids == 0
        ),
        "duplicate_same_label_pair_classes_only_model_cross_group_shared_rows": (
            unmodeled_duplicate_pair_classes == 0
        ),
        "zero_repeated_physical_rows_within_groups": True,
        "zero_shared_physical_row_identity_content_drift": True,
        "zero_conflicting_pair_classes": conflicting_pair_classes == 0,
        "zero_partial_groups": partial_groups == 0,
        "n19_forbidden": n19_rows == 0,
        "n25_retained_share_at_most_one_quarter": n25_rows * 4 <= len(retained_ids),
        "requested_positive_output_nonzero": (
            not requested_positive_operations or retained_positive_rows > 0
        ),
        "requested_negative_output_nonzero": (
            not (requested_negative_operations or spec.orbit_operations)
            or retained_negative_rows > 0
        ),
        "requested_new_wave3_negative_family_minimum_met": (
            len(useful_new_negative_families) >= required_useful_new_negative_families
        ),
        "requested_wave4_closure_output_nonzero": (
            not spec.orbit_operations or complete_wave4_closure_groups > 0
        ),
    }
    return {
        "roots": len(sources),
        "passed_roots": len(passed_roots),
        "failed_roots": len(failed_roots),
        "logical_pair_references": len(pairs),
        "emitted_rows": len(physical_by_id),
        "shared_group_row_references": modeled_stable_id_references,
        "retained_rows_after_n25_cap": len(retained_ids),
        "retained_unique_ancestry_roots": len(
            {str(pair["root_id"]) for pair in retained_logical_pairs}
        ),
        "pairs_automatically_structurally_validated": structurally_validated_pairs,
        "wrong_labels": wrong_labels,
        "self_pairs": self_pairs,
        "duplicate_stable_ids": duplicate_stable_ids,
        "duplicate_stable_id_classes": duplicate_stable_id_classes,
        "modeled_cross_group_stable_id_references": modeled_stable_id_references,
        "unmodeled_duplicate_stable_ids": unmodeled_duplicate_stable_ids,
        "duplicate_pair_classes": duplicate_pair_classes,
        "duplicate_same_label_pair_classes": duplicate_same_label_pair_classes,
        "modeled_cross_group_pair_class_references": modeled_pair_class_references,
        "unmodeled_duplicate_pair_classes": unmodeled_duplicate_pair_classes,
        "repeated_physical_rows_within_groups": 0,
        "shared_physical_row_identity_content_drift": 0,
        "conflicting_pair_classes": conflicting_pair_classes,
        "partial_groups": partial_groups,
        "complete_wave4_closure_groups": complete_wave4_closure_groups,
        "n19_rows": n19_rows,
        "n25_retained_rows": n25_rows,
        "n25_retained_share": round(n25_share, 8),
        "n25_share_cap": cap.report.record(),
        "emitted_pairs_sha256": hash_canonical(
            sorted((row_id, *identity) for row_id, identity in physical_by_id.items())
        ),
        "retained_pair_ids_sha256": hash_canonical(sorted(retained_ids)),
        "requested_output_requirements": {
            "positive_operations": requested_positive_operations,
            "negative_operations": requested_negative_operations,
            "new_wave3_negative_families": requested_new_negative_families,
            "required_useful_new_wave3_negative_families": (required_useful_new_negative_families),
            "useful_new_wave3_negative_families": useful_new_negative_families,
            "wave4_operations": list(spec.orbit_operations),
        },
        "per_source_yield": source_yields,
        "per_operation_yield": _typed_gate_yield_table(pairs, retained_ids),
        "per_negative_family_yield": negative_yields,
        "per_preserving_family_emitted_rows": dict(sorted(preserving_rows.items())),
        "pair_delta_cells": dict(sorted(pair_delta_cells.items())),
        "label_counts": dict(sorted(Counter(str(pair["label"]) for pair in pairs).items())),
        "retained_label_counts": {
            "false": retained_negative_rows,
            "true": retained_positive_rows,
        },
        "checks": checks,
    }


class CompilerTypedCertificateGateRunner:
    """Durable exact-certificate gate over the frozen compiler audit sample."""

    def __init__(
        self,
        settings: CompilerAuditSettings,
        spec: CompilerTypedHookSpec,
        policy: OrbitPolicy,
        *,
        executor_factory: TypedCertificateExecutorFactory | None = None,
        backend_factory: BackendFactory = _default_backend_factory,
        owner_session: str = "codex-sft1-wave5-typed-gate",
        manage_resources: bool = True,
        verify_project: bool = True,
        required_sample_rows: int = TYPED_CERTIFICATE_GATE_REQUIRED_ROOTS,
    ) -> None:
        if required_sample_rows <= 0:
            raise ValueError("typed certificate gate sample size must be positive")
        if settings.expected_rows != required_sample_rows:
            raise CompilerReplayError(
                "typed certificate gate settings do not name the exact required sample size"
            )
        if "N19_WHOLE_CLAIM_NEGATION_V1" in spec.operations:
            raise CompilerReplayError("typed certificate gate forbids N19")
        if policy.maximum_depth < spec.maximum_depth:
            raise CompilerReplayError("typed certificate depth exceeds the Wave 4 policy")
        if policy.maximum_variants_per_root < spec.maximum_variants_per_orbit:
            raise CompilerReplayError("typed certificate variant bound exceeds Wave 4 policy")
        self.settings = settings
        self.spec = spec
        self.policy = policy
        self.owner_session = owner_session
        self.manage_resources = manage_resources
        self.verify_project = verify_project
        self.required_sample_rows = required_sample_rows
        self.gate_root = typed_certificate_gate_root(settings)
        self.complete_path = typed_certificate_gate_complete_path(settings)
        self.status_path = self.gate_root / "status.json"
        self.journal = Journal(self.gate_root / "journal.jsonl")
        self.gold = GoldBlocklist.load(
            settings.inventory.gold_blocklist_path,
            expected_sha256=settings.inventory.gold_blocklist_sha256,
        )
        self.engine_version = engine_semantic_version(find_repo_root(settings.engine_path))
        if executor_factory is None:
            self.executor_factory: TypedCertificateExecutorFactory = lambda: (
                _PersistentTypedCertificateExecutor(
                    settings,
                    spec,
                    policy,
                    journal=self.journal,
                    backend_factory=backend_factory,
                )
            )
        else:
            self.executor_factory = executor_factory

    def _run_spec_path(self, run_id: str) -> Path:
        return self.gate_root / "runs" / run_id / "run_spec.json"

    def _replay_path(self, run_id: str) -> Path:
        return self.gate_root / "runs" / run_id / TYPED_CERTIFICATE_GATE_REPLAY

    def _run_terminal_path(self, run_id: str) -> Path:
        return self.gate_root / "runs" / run_id / "terminal.json"

    def _ensure_run_spec(
        self, sample: Sequence[Mapping[str, Any]], *, write_if_absent: bool
    ) -> tuple[str, dict[str, Any], Path]:
        if len(sample) != self.required_sample_rows:
            raise CompilerReplayError(
                f"typed certificate gate requires exactly {self.required_sample_rows} roots"
            )
        identity = _typed_gate_run_identity(
            self.settings,
            self.spec,
            self.policy,
            sample,
            required_sample_rows=self.required_sample_rows,
        )
        if identity["engine_semantic_version"] != self.engine_version:
            raise CompilerReplayError("typed certificate engine identity changed during setup")
        run_id = hash_canonical(identity)
        payload: dict[str, Any] = {"run_id": run_id, **identity}
        path = self._run_spec_path(run_id)
        data = canonical_json_bytes(payload) + b"\n"
        if write_if_absent:
            _write_exact(path, data)
        elif not path.is_file() or path.is_symlink() or path.read_bytes() != data:
            raise CompilerReplayError("typed certificate gate run spec is absent or differs")
        return run_id, payload, path

    def _load_inputs(
        self, *, write_run_spec: bool
    ) -> tuple[
        tuple[dict[str, Any], ...],
        tuple[CompilerAuditSource, ...],
        str,
        dict[str, Any],
        Path,
    ]:
        sample = load_audit_sample(self.settings)
        run_id, run_spec, run_spec_path = self._ensure_run_spec(
            sample, write_if_absent=write_run_spec
        )
        shards = load_pinned_input_shards(self.settings.inventory)
        sources = resolve_audit_sources(self.settings, sample, shards=shards)
        if [source.root_id for source in sources] != [str(row["root_id"]) for row in sample]:
            raise CompilerReplayError("typed certificate source resolution changes sample order")
        return sample, sources, run_id, run_spec, run_spec_path

    def _journal_terminals(self, run_id: str) -> dict[str, dict[str, Any]]:
        terminals: dict[str, dict[str, Any]] = {}
        fields = (
            "run_id",
            "root_id",
            "cache_key",
            "cache_sha256",
            "status",
            "taxonomy",
        )
        for record in self.journal.read():
            if record.get("event") != "typed_root_terminal" or record.get("run_id") != run_id:
                continue
            root_id = str(record.get("root_id", ""))
            prior = terminals.get(root_id)
            if prior is not None and {field: prior.get(field) for field in fields} != {
                field: record.get(field) for field in fields
            }:
                raise CompilerReplayError(
                    f"conflicting typed certificate journal terminals for {root_id}"
                )
            terminals[root_id] = record
        return terminals

    def _ensure_journal_terminal(
        self,
        source: CompilerAuditSource,
        record: Mapping[str, Any],
        path: Path,
        *,
        run_id: str,
        allow_append: bool,
    ) -> None:
        expected = {
            "event": "typed_root_terminal",
            "run_id": run_id,
            "root_id": source.root_id,
            "cache_key": record["cache_key"],
            "cache_sha256": hash_file(path),
            "status": record["status"],
            "taxonomy": record["taxonomy"],
        }
        existing = self._journal_terminals(run_id).get(source.root_id)
        if existing is None:
            if not allow_append:
                raise CompilerReplayError(
                    f"typed certificate journal omits terminal {source.root_id}"
                )
            self.journal.append(expected)
            return
        for field, value in expected.items():
            if existing.get(field) != value:
                raise CompilerReplayError(
                    f"typed certificate journal/cache disagree for {source.root_id}"
                )

    def _persist_terminal(
        self,
        source: CompilerAuditSource,
        record: Mapping[str, Any],
        *,
        run_id: str,
    ) -> tuple[dict[str, Any], Path]:
        path = _typed_gate_cache_path(self.settings, str(record["cache_key"]))
        _write_exact(path, canonical_json_bytes(record) + b"\n")
        self._ensure_journal_terminal(source, record, path, run_id=run_id, allow_append=True)
        return dict(record), path

    def _validate_cached(
        self,
        source: CompilerAuditSource,
        *,
        run_id: str,
        allow_journal_append: bool,
    ) -> tuple[dict[str, Any], Path] | None:
        cached = _load_typed_gate_cache(
            source,
            run_id=run_id,
            settings=self.settings,
            spec=self.spec,
            policy=self.policy,
        )
        if cached is None:
            return None
        record, path = cached
        _validate_typed_gate_cache_record(
            source,
            record,
            settings=self.settings,
            spec=self.spec,
            policy=self.policy,
            gold=self.gold,
            engine_version=self.engine_version,
        )
        self._ensure_journal_terminal(
            source,
            record,
            path,
            run_id=run_id,
            allow_append=allow_journal_append,
        )
        return record, path

    def _record_from_outcome(
        self,
        source: CompilerAuditSource,
        outcome: CompilerTypedCertificateRootOutcome,
        *,
        run_id: str,
    ) -> dict[str, Any]:
        if outcome.root_id != source.root_id:
            raise CompilerReplayError("typed executor attached evidence to a different root")
        if outcome.status != "passed":
            return _typed_gate_terminal_record(
                source,
                run_id=run_id,
                settings=self.settings,
                spec=self.spec,
                policy=self.policy,
                status="failed",
                taxonomy=outcome.taxonomy or "typed_executor_rejected",
                outcome=outcome,
                validation_summary=None,
                pairs=(),
            )
        try:
            summary, pairs = _validate_typed_gate_evidence(
                source,
                outcome,
                settings=self.settings,
                spec=self.spec,
                policy=self.policy,
                gold=self.gold,
                engine_version=self.engine_version,
            )
        except (CompilerReplayError, GoalV1Error, OrbitError, SprintEngineError, ValueError) as exc:
            return _typed_gate_terminal_record(
                source,
                run_id=run_id,
                settings=self.settings,
                spec=self.spec,
                policy=self.policy,
                status="failed",
                taxonomy=f"certificate_validation:{type(exc).__name__}:{str(exc)[:700]}",
                outcome=outcome,
                validation_summary=None,
                pairs=(),
            )
        return _typed_gate_terminal_record(
            source,
            run_id=run_id,
            settings=self.settings,
            spec=self.spec,
            policy=self.policy,
            status="passed",
            taxonomy="typed_certificates_checked",
            outcome=outcome,
            validation_summary=summary,
            pairs=pairs,
        )

    def _validated_state(
        self,
        sources: Sequence[CompilerAuditSource],
        *,
        run_id: str,
        allow_journal_append: bool,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], Counter[str]]:
        records: list[dict[str, Any]] = []
        receipts: list[dict[str, Any]] = []
        taxonomy: Counter[str] = Counter()
        for source in sources:
            cached = self._validate_cached(
                source,
                run_id=run_id,
                allow_journal_append=allow_journal_append,
            )
            if cached is None:
                raise CompilerReplayError(
                    f"typed certificate root lacks terminal cache: {source.root_id}"
                )
            record, path = cached
            records.append(record)
            taxonomy[str(record["taxonomy"])] += 1
            receipts.append(
                {
                    "root_id": source.root_id,
                    "cache_key": record["cache_key"],
                    "cache_path": str(path),
                    "cache_sha256": hash_file(path),
                    "status": record["status"],
                    "validation_summary_sha256": (
                        hash_canonical(record["validation_summary"])
                        if record["validation_summary"] is not None
                        else None
                    ),
                }
            )
        receipts.sort(key=lambda item: str(item["root_id"]))
        return records, receipts, taxonomy

    def _attempt_totals(self, run_id: str) -> dict[str, Any]:
        totals: Counter[str] = Counter()
        wall_seconds = 0.0
        peak_rss = 0
        invocations = 0
        for record in self.journal.read():
            if (
                record.get("event") != "typed_gate_invocation_complete"
                or record.get("run_id") != run_id
            ):
                continue
            invocations += 1
            for field in (
                "lean_requests",
                "lean_elapsed_ms",
                "batch_attempts",
                "roots_submitted",
            ):
                totals[field] += int(record.get(field, 0))
            wall_seconds += float(record.get("backend_wall_seconds", 0.0))
            peak_rss = max(peak_rss, int(record.get("peak_rss_bytes", 0)))
        return {
            "invocations_with_terminal_event": invocations,
            "lean_requests": totals["lean_requests"],
            "lean_elapsed_ms": totals["lean_elapsed_ms"],
            "backend_wall_seconds": round(wall_seconds, 6),
            "batch_attempts": totals["batch_attempts"],
            "roots_submitted": totals["roots_submitted"],
            "peak_rss_bytes": peak_rss,
        }

    def _replay_payload(
        self,
        *,
        run_id: str,
        run_spec_path: Path,
        sources: Sequence[CompilerAuditSource],
        receipts: Sequence[Mapping[str, Any]],
        integrity: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            "artifact_kind": "sft1_wave5_typed_certificate_forced_resume_replay",
            "schema_version": TYPED_CERTIFICATE_GATE_REPLAY_VERSION,
            "run_id": run_id,
            "forced_resume": True,
            "run_spec_sha256": hash_file(run_spec_path),
            "inventory_manifest_sha256": hash_file(self.settings.inventory_manifest_path),
            "audit_sample_sha256": hash_file(self.settings.sample_path),
            "roots_verified": len(sources),
            "passed_roots": int(integrity["passed_roots"]),
            "failed_roots": int(integrity["failed_roots"]),
            "cache_hits": len(sources),
            "lean_requests": 0,
            "backend_constructed": False,
            "resource_claimed": False,
            "cache_receipts_sha256": hash_canonical(receipts),
            "integrity_sha256": hash_canonical(integrity),
            "all_checks_passed": all(
                bool(value)
                for value in _mapping(integrity["checks"], "typed integrity checks").values()
            ),
        }

    def _terminal_payload(
        self,
        *,
        run_id: str,
        run_spec: Mapping[str, Any],
        run_spec_path: Path,
        sources: Sequence[CompilerAuditSource],
        receipts: Sequence[Mapping[str, Any]],
        taxonomy: Mapping[str, int],
        integrity: Mapping[str, Any],
        replay_path: Path,
        replay: Mapping[str, Any],
        last_invocation: Mapping[str, Any],
    ) -> dict[str, Any]:
        checks = _mapping(integrity["checks"], "typed certificate integrity checks")
        passed = (
            int(integrity["passed_roots"]) == len(sources)
            and all(bool(value) for value in checks.values())
            and replay.get("lean_requests") == 0
            and replay.get("backend_constructed") is False
            and replay.get("forced_resume") is True
        )
        binding_fields = (
            "audit_sample_sha256",
            "audit_sample_receipt_sha256",
            "inventory_manifest_sha256",
            "inventory_run_spec_sha256",
            "audit_config_sha256",
            "audit_config_semantic_hash",
            "engine_source_sha256",
            "engine_semantic_version",
            "typed_spec_hash",
            "typed_spec",
            "wave4_policy_hash",
            "wave4_policy",
            "checker_hash",
            "checker",
            "semantic_dependency_sha256",
            "source_pin",
            "project",
        )
        return {
            "artifact_kind": "sft1_wave5_typed_certificate_gate_terminal",
            "schema_version": TYPED_CERTIFICATE_GATE_SCHEMA_VERSION,
            "run_id": run_id,
            "status": "passed" if passed else "failed",
            "run_spec_path": str(run_spec_path),
            "run_spec_sha256": hash_file(run_spec_path),
            "required_sample_rows": self.required_sample_rows,
            "roots": len(sources),
            "passed_roots": integrity["passed_roots"],
            "failed_roots": integrity["failed_roots"],
            "bindings": {field: run_spec[field] for field in binding_fields},
            "cache_receipts": [dict(receipt) for receipt in receipts],
            "cache_receipts_sha256": hash_canonical(receipts),
            "failure_taxonomy": dict(sorted(taxonomy.items())),
            "integrity": dict(integrity),
            "checks": checks,
            "execution_totals": self._attempt_totals(run_id),
            "last_invocation": dict(last_invocation),
            "forced_resume_replay_path": str(replay_path),
            "forced_resume_replay_sha256": hash_file(replay_path),
            "forced_resume_replay": dict(replay),
            "automated_validation_verdict": (
                "exhaustive_structural_and_certificate_validation_passed"
                if passed
                else "exhaustive_structural_and_certificate_validation_failed"
            ),
            "manual_inspection_verdict": "not_recorded",
            "proof_contract": {
                "exact_prefix_plus_literal_by_plus_body": True,
                "source_label_true": True,
                "source_proofs_meta_and_kernel_checked": checks["all_source_proofs_checked"],
                "wave3_exact_certificates_checked": checks["all_wave3_certificates_exact"],
                "wave4_exact_closure_checked": checks["all_wave4_closures_exact"],
                "candidate_and_reference_screens_checked": checks[
                    "candidate_and_reference_screens_passed"
                ],
                "all_pairs_automatically_structurally_validated": checks[
                    "all_emitted_pairs_automatically_structurally_validated"
                ],
                "manual_review_required_by_typed_gate": False,
                "proof_certified_core_only": True,
                "lower_confidence_rows": 0,
            },
        }

    def _validate_terminal(
        self,
        terminal: Mapping[str, Any],
        *,
        sources: Sequence[CompilerAuditSource],
        run_id: str,
        run_spec: Mapping[str, Any],
        run_spec_path: Path,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        records, receipts, taxonomy = self._validated_state(
            sources,
            run_id=run_id,
            allow_journal_append=False,
        )
        integrity = _typed_gate_integrity(
            sources,
            records,
            spec=self.spec,
            policy=self.policy,
        )
        replay_path = self._replay_path(run_id)
        if not replay_path.is_file() or replay_path.is_symlink():
            raise CompilerReplayError("typed certificate forced-resume receipt is absent")
        replay = read_json_object(replay_path)
        expected_replay = self._replay_payload(
            run_id=run_id,
            run_spec_path=run_spec_path,
            sources=sources,
            receipts=receipts,
            integrity=integrity,
        )
        if replay != expected_replay:
            raise CompilerReplayError("typed certificate forced-resume receipt differs")
        last_invocation = _mapping(
            terminal.get("last_invocation"), "typed certificate last invocation"
        )
        expected_terminal = self._terminal_payload(
            run_id=run_id,
            run_spec=run_spec,
            run_spec_path=run_spec_path,
            sources=sources,
            receipts=receipts,
            taxonomy=taxonomy,
            integrity=integrity,
            replay_path=replay_path,
            replay=replay,
            last_invocation=last_invocation,
        )
        if terminal != expected_terminal:
            raise CompilerReplayError("typed certificate gate terminal differs from replay")
        run_terminal_path = self._run_terminal_path(run_id)
        if not run_terminal_path.is_file() or run_terminal_path.is_symlink():
            raise CompilerReplayError("typed certificate content-addressed terminal is absent")
        run_terminal = read_json_object(run_terminal_path)
        if run_terminal != terminal:
            raise CompilerReplayError("typed certificate terminal copies disagree")
        return replay, integrity

    def _public_verification(
        self,
        terminal: Mapping[str, Any],
        replay: Mapping[str, Any],
        integrity: Mapping[str, Any],
    ) -> dict[str, Any]:
        if terminal.get("status") != "passed":
            raise CompilerReplayError("typed certificate gate terminal did not pass")
        checks = _mapping(terminal.get("checks"), "typed certificate terminal checks")
        if not checks or not all(value is True for value in checks.values()):
            raise CompilerReplayError("typed certificate gate has a false check")
        if (
            replay.get("forced_resume") is not True
            or replay.get("lean_requests") != 0
            or replay.get("backend_constructed") is not False
            or replay.get("resource_claimed") is not False
        ):
            raise CompilerReplayError("typed certificate replay is not zero-call and backend-free")
        bindings = _mapping(terminal.get("bindings"), "typed certificate terminal bindings")
        return {
            "passed": True,
            "schema_version": TYPED_CERTIFICATE_GATE_SCHEMA_VERSION,
            "run_id": terminal["run_id"],
            "terminal_path": str(self.complete_path),
            "terminal_sha256": hash_file(self.complete_path),
            "audit_sample_path": str(self.settings.sample_path),
            "audit_sample_sha256": bindings["audit_sample_sha256"],
            "audit_sample_rows": terminal["roots"],
            "inventory_manifest_path": str(self.settings.inventory_manifest_path),
            "inventory_manifest_sha256": bindings["inventory_manifest_sha256"],
            "audit_config_semantic_hash": bindings["audit_config_semantic_hash"],
            "engine_source_sha256": bindings["engine_source_sha256"],
            "engine_semantic_version": bindings["engine_semantic_version"],
            "typed_spec_hash": bindings["typed_spec_hash"],
            "typed_spec": bindings["typed_spec"],
            "wave4_policy_hash": bindings["wave4_policy_hash"],
            "checker_hash": bindings["checker_hash"],
            "checks": checks,
            "integrity": dict(integrity),
            "execution": terminal["execution_totals"],
            "automated_validation_verdict": terminal["automated_validation_verdict"],
            "manual_inspection_verdict": terminal["manual_inspection_verdict"],
            "replay": dict(replay),
        }

    def verify(self) -> dict[str, Any]:
        """Read and fully replay the canonical pass marker without constructing Lean."""

        _sample, sources, run_id, run_spec, run_spec_path = self._load_inputs(write_run_spec=False)
        if not self.complete_path.is_file() or self.complete_path.is_symlink():
            raise CompilerReplayError("typed certificate gate pass marker is absent")
        terminal = read_json_object(self.complete_path)
        replay, integrity = self._validate_terminal(
            terminal,
            sources=sources,
            run_id=run_id,
            run_spec=run_spec,
            run_spec_path=run_spec_path,
        )
        return self._public_verification(terminal, replay, integrity)

    def _execute_missing(
        self,
        missing: Sequence[CompilerAuditSource],
        *,
        run_id: str,
    ) -> tuple[CompilerTypedCertificateExecution, int]:
        if self.verify_project:
            _verify_project(self.settings)
        with _resource_claim(
            self.settings,
            owner_session=self.owner_session,
            enabled=self.manage_resources,
        ):
            with _RssSampler() as sampler:
                executor = self.executor_factory()
                try:
                    execution = executor.execute(missing, run_id=run_id)
                finally:
                    executor.close()
            peak_rss = sampler.peak
        if peak_rss > int(self.settings.lean_rss_claim_gib * 1024**3):
            raise CompilerReplayInfrastructureError(
                "typed certificate gate exceeded its measured RSS reservation"
            )
        if (
            execution.lean_requests < 0
            or execution.lean_elapsed_ms < 0
            or execution.backend_wall_seconds < 0
            or execution.batch_attempts < 0
        ):
            raise CompilerReplayError("typed certificate executor returned negative metrics")
        expected_ids = [source.root_id for source in missing]
        outcome_ids = [outcome.root_id for outcome in execution.outcomes]
        if len(outcome_ids) != len(set(outcome_ids)) or set(outcome_ids) != set(expected_ids):
            raise CompilerReplayError("typed certificate executor root coverage differs")
        by_root = {outcome.root_id: outcome for outcome in execution.outcomes}
        for source in missing:
            record = self._record_from_outcome(source, by_root[source.root_id], run_id=run_id)
            self._persist_terminal(source, record, run_id=run_id)
        self.journal.append(
            {
                "event": "typed_gate_invocation_complete",
                "run_id": run_id,
                "roots_submitted": len(missing),
                "lean_requests": execution.lean_requests,
                "lean_elapsed_ms": execution.lean_elapsed_ms,
                "backend_wall_seconds": execution.backend_wall_seconds,
                "batch_attempts": execution.batch_attempts,
                "peak_rss_bytes": peak_rss,
            }
        )
        return execution, peak_rss

    def run(self) -> CompilerTypedCertificateGateResult:
        sample, sources, run_id, run_spec, run_spec_path = self._load_inputs(write_run_spec=True)
        del sample
        if self.complete_path.is_file():
            verified = self.verify()
            integrity = _mapping(verified["integrity"], "typed certificate integrity")
            return CompilerTypedCertificateGateResult(
                run_id=run_id,
                complete_path=self.complete_path,
                status="passed",
                roots=len(sources),
                passed_roots=int(integrity["passed_roots"]),
                failed_roots=int(integrity["failed_roots"]),
                cache_hits=len(sources),
                lean_requests=0,
            )
        existing_terminal_path = self._run_terminal_path(run_id)
        if existing_terminal_path.is_file():
            terminal = read_json_object(existing_terminal_path)
            replay, integrity = self._validate_terminal(
                terminal,
                sources=sources,
                run_id=run_id,
                run_spec=run_spec,
                run_spec_path=run_spec_path,
            )
            del replay
            return CompilerTypedCertificateGateResult(
                run_id=run_id,
                complete_path=self.complete_path,
                status=cast(Literal["passed", "failed"], terminal["status"]),
                roots=len(sources),
                passed_roots=int(integrity["passed_roots"]),
                failed_roots=int(integrity["failed_roots"]),
                cache_hits=len(sources),
                lean_requests=0,
            )

        cache_hits = 0
        preflight_failures = 0
        missing: list[CompilerAuditSource] = []
        for source in sources:
            cached = self._validate_cached(
                source,
                run_id=run_id,
                allow_journal_append=True,
            )
            if cached is not None:
                cache_hits += 1
                continue
            reason = _preflight_reason(source)
            if reason is None:
                missing.append(source)
                continue
            preflight_failures += 1
            record = _typed_gate_terminal_record(
                source,
                run_id=run_id,
                settings=self.settings,
                spec=self.spec,
                policy=self.policy,
                status="failed",
                taxonomy=f"preflight:{reason}",
                outcome=None,
                validation_summary=None,
                pairs=(),
            )
            self._persist_terminal(source, record, run_id=run_id)

        execution = CompilerTypedCertificateExecution(
            outcomes=(),
            lean_requests=0,
            lean_elapsed_ms=0,
            backend_wall_seconds=0.0,
            batch_attempts=0,
        )
        peak_rss = 0
        if missing:
            execution, peak_rss = self._execute_missing(missing, run_id=run_id)
        last_invocation = {
            "cache_hits": cache_hits,
            "preflight_failures": preflight_failures,
            "roots_submitted": len(missing),
            "lean_requests": execution.lean_requests,
            "lean_elapsed_ms": execution.lean_elapsed_ms,
            "backend_wall_seconds": execution.backend_wall_seconds,
            "batch_attempts": execution.batch_attempts,
            "peak_rss_bytes": peak_rss,
        }
        records, receipts, taxonomy = self._validated_state(
            sources,
            run_id=run_id,
            allow_journal_append=False,
        )
        integrity = _typed_gate_integrity(
            sources,
            records,
            spec=self.spec,
            policy=self.policy,
        )
        replay = self._replay_payload(
            run_id=run_id,
            run_spec_path=run_spec_path,
            sources=sources,
            receipts=receipts,
            integrity=integrity,
        )
        replay_path = self._replay_path(run_id)
        _write_exact(replay_path, canonical_json_bytes(replay) + b"\n")
        terminal = self._terminal_payload(
            run_id=run_id,
            run_spec=run_spec,
            run_spec_path=run_spec_path,
            sources=sources,
            receipts=receipts,
            taxonomy=taxonomy,
            integrity=integrity,
            replay_path=replay_path,
            replay=replay,
            last_invocation=last_invocation,
        )
        _write_exact(
            existing_terminal_path,
            canonical_json_bytes(terminal) + b"\n",
        )
        if terminal["status"] == "passed":
            _write_exact(self.complete_path, canonical_json_bytes(terminal) + b"\n")
        write_atomic(self.status_path, canonical_json_bytes(terminal) + b"\n")
        return CompilerTypedCertificateGateResult(
            run_id=run_id,
            complete_path=self.complete_path,
            status=cast(Literal["passed", "failed"], terminal["status"]),
            roots=len(sources),
            passed_roots=int(integrity["passed_roots"]),
            failed_roots=int(integrity["failed_roots"]),
            cache_hits=cache_hits,
            lean_requests=execution.lean_requests,
        )

    def replay(self) -> dict[str, Any]:
        """Validate the durable forced-resume receipt without constructing Lean."""

        verified = self.verify()
        return _mapping(verified["replay"], "typed certificate replay")


def verify_typed_certificate_gate(
    settings: CompilerAuditSettings,
    spec: CompilerTypedHookSpec,
    policy: OrbitPolicy,
    *,
    required_sample_rows: int = TYPED_CERTIFICATE_GATE_REQUIRED_ROOTS,
) -> dict[str, Any]:
    """Verify the canonical typed gate and return the Wave 5 scale handoff."""

    return CompilerTypedCertificateGateRunner(
        settings,
        spec,
        policy,
        executor_factory=_never_executor,
        manage_resources=False,
        verify_project=False,
        required_sample_rows=required_sample_rows,
    ).verify()


def _never_executor() -> CompilerTypedCertificateExecutor:
    raise AssertionError("typed certificate verification must never construct an executor")


def _typed_gate_result_json(
    result: CompilerTypedCertificateGateResult,
) -> dict[str, object]:
    return {
        "run_id": result.run_id,
        "complete_path": str(result.complete_path),
        "status": result.status,
        "roots": result.roots,
        "passed_roots": result.passed_roots,
        "failed_roots": result.failed_roots,
        "cache_hits": result.cache_hits,
        "lean_requests": result.lean_requests,
    }
