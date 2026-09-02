"""Focused v3 repair after shard 1 (additive; historical v2 artifacts stay immutable).

Shard 1 failed only the raw Lean-invalid gate, and the failures were attributed to two
pipeline defects: candidates that copied pretty-printer-only inaccessible binder names such as
``inst✝`` from the rendered reference, and candidate commands that re-emitted the census's
alphabetically flattened plain ``open_context``. This module owns the repair's bounded
evidence and freezing surfaces:

* the deterministic 20-root canary sample that targets both defect classes, its provider config
  (role ``canary``), and the chained v3 provider configs for the frozen shards 2-10;
* the zero-provider adversarial check on twelve shard-1 roots (six dagger-heavy, six
  open/namespace/scoped-context cases) that requires unchanged canonical reference identities,
  no inaccessible names in authoring views, and zero prelude diagnostics;
* the zero-provider re-elaboration of every historical shard-1 Lean failure classified as an
  open-rendering failure through the v3 oracle, requiring zero prelude-attributed failures while
  genuine candidate-local errors stay invalid.

Nothing here reruns shard 1, recertifies references, refreezes REPR, or regenerates accepted rows.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.host_resources import ReservationError, claim_resources, release_resources
from leanfaith.sft2a.certified_sample_v52 import _replacement_row
from leanfaith.sft2a.config import LoadedSFT2AConfig, load_sft2a_config
from leanfaith.sft2a.lean_oracle import (
    AUTHORING_VIEW_VERSION_V3,
    COMMAND_TEMPLATE_VERSION_V3,
    EFFECTIVE_CONTEXT_VERSION_V3,
    INACCESSIBLE_NAME_MARK,
    ORACLE_METHOD_VERSION_V3,
    elaborator_sha256,
)
from leanfaith.sft2a.legacy import _atomic_exact
from leanfaith.sft2a.models import OneRootConfig, SFT2AV52Config
from leanfaith.sft2a.pipeline import _canonical_level_params, _closed_expr_hash
from leanfaith.sft2a.provider_rehearsal_v52 import (
    OraclePool,
    PooledOracle,
    _atomic_replace_json,
    _object,
    _repo_path,
    certified_reference_result_v52,
    load_provider_rehearsal_v52,
)
from leanfaith.sft2a.sprint_pilot_v52 import (
    SPRINT_PILOT_VERSION,
    _append_stage,
    sprint_capacity_check,
)
from leanfaith.sft2a.sprint_scale_v52 import (
    SHARD_LEAN_RSS_GIB_PER_WORKER,
    LoadedSprintPoolConfig,
    _jsonl,
    _jsonl_bytes,
    _plan_shard_rotation,
    _result_path,
    _screen_certified,
    load_sprint_pool_config,
)

REPAIR_PLAN_VERSION = "leanfaith_sft2a_sprint_repair_v3_plan_v1"
CANARY_MANIFEST_VERSION = "leanfaith_sft2a_sprint_canary_v3_manifest_v1"
SHARDS_V3_MANIFEST_VERSION = "leanfaith_sft2a_sprint_shards_v3_manifest_v1"
REPAIR_GATES_VERSION = "leanfaith_sft2a_sprint_repair_v3_gates_v1"
_SOURCES = ("mathlib", "physlib", "cslib", "compiler_data")
_OPEN_FAILURE_PATTERNS = (
    re.compile(r"^unknown namespace"),
    re.compile(r"unexpected token 'hiding'"),
)


class SprintRepairV3Error(RuntimeError):
    """A repair plan, canary freeze, adversarial check, or re-elaboration invariant failed."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class LoadedRepairPlanV3:
    path: Path
    document: dict[str, object]
    sha256: str
    repo_root: Path
    pool: LoadedSprintPoolConfig
    base: LoadedSFT2AConfig
    base_config_path: Path
    historical_shard_run_root: Path
    historical_shard_sample_path: Path
    historical_shards_manifest_path: Path
    repair_gates_output_root: Path
    canary: dict[str, object]
    shards_v3: dict[str, object]
    oracle_v3_gate_receipt_path: Path


def load_repair_plan_v3(path: Path) -> LoadedRepairPlanV3:
    """Load the additive v3 repair plan with its pins (zero Lean, zero provider)."""

    resolved = path.resolve()
    document = _object(resolved)
    if document.get("version") != REPAIR_PLAN_VERSION:
        raise SprintRepairV3Error("sprint repair plan version differs")
    repo_root = Path(__file__).resolve().parents[3]
    pool = load_sprint_pool_config(_repo_path(repo_root, document.get("pool_config_path")))
    base_path = _repo_path(repo_root, document.get("base_config_path"))
    if hash_file(base_path) != document.get("base_config_sha256"):
        raise SprintRepairV3Error("repair plan base config hash differs")
    base = load_sft2a_config(base_path, verify_binaries=False)
    if not isinstance(base.config, SFT2AV52Config):
        raise SprintRepairV3Error("repair plan base config is not v5.2")
    if "{{AUTHORING_VIEW}}" not in base.proposer_prompt:
        raise SprintRepairV3Error("repair plan base config must bind the v3 authoring prompt")
    policy = _repo_path(repo_root, document.get("labeling_defaults_policy_path"))
    if hash_file(policy) != document.get("labeling_defaults_policy_sha256"):
        raise SprintRepairV3Error("repair plan labeling policy hash differs")
    historical_run = Path(str(document.get("historical_shard_run_root")))
    historical_sample = Path(str(document.get("historical_shard_sample_path")))
    shards_manifest = Path(str(document.get("historical_shards_manifest_path")))
    for required in (historical_run, historical_sample, shards_manifest):
        if not required.exists():
            raise SprintRepairV3Error(f"repair plan historical artifact is absent: {required}")
    canary = document.get("canary")
    shards_v3 = document.get("shards_v3")
    if not isinstance(canary, dict) or not isinstance(shards_v3, dict):
        raise SprintRepairV3Error("repair plan lacks the canary or shards_v3 contract")
    for key in ("dagger_roots", "context_roots", "provider_concurrency", "kimi_audit_rows"):
        value = canary.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise SprintRepairV3Error(f"repair plan canary field {key} is malformed")
    if canary.get("selection", "defect_classes") == "natural_mix":
        natural_mix = canary.get("natural_source_mix")
        if not isinstance(natural_mix, dict) or sum(natural_mix.values()) != 20:
            raise SprintRepairV3Error("a natural-mix canary needs natural_source_mix summing to 20")
    elif int(cast(int, canary["dagger_roots"])) + int(cast(int, canary["context_roots"])) != 20:
        raise SprintRepairV3Error("the canary is exactly 20 roots across both defect classes")
    for key in ("first_shard", "last_shard"):
        value = shards_v3.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 10:
            raise SprintRepairV3Error(f"repair plan shards_v3 field {key} is malformed")
    if int(cast(int, shards_v3["first_shard"])) < 2:
        raise SprintRepairV3Error("the v3 path never reruns shard 1")
    gate_path = document.get("oracle_v3_gate_receipt_path")
    if not isinstance(gate_path, str) or not gate_path:
        raise SprintRepairV3Error("repair plan must name oracle_v3_gate_receipt_path")
    for flag in (
        "legacy_rejudge_authorized",
        "publication_authorized",
        "scale_50k_authorized",
        "training_authorized",
    ):
        if document.get(flag) is not False:
            raise SprintRepairV3Error(f"repair plan must set {flag} to false")
    return LoadedRepairPlanV3(
        path=resolved,
        document=document,
        sha256=hash_file(resolved),
        repo_root=repo_root,
        pool=pool,
        base=base,
        base_config_path=base_path,
        historical_shard_run_root=historical_run,
        historical_shard_sample_path=historical_sample,
        historical_shards_manifest_path=shards_manifest,
        repair_gates_output_root=Path(str(document.get("repair_gates_output_root"))),
        canary=dict(canary),
        shards_v3=dict(shards_v3),
        oracle_v3_gate_receipt_path=Path(gate_path),
    )


# --------------------------------------------------------------------------------------------
# Deterministic defect-class selection (pure functions)
# --------------------------------------------------------------------------------------------


def dagger_count(goal: str) -> int:
    return goal.count(INACCESSIBLE_NAME_MARK)


def context_risk(context: Mapping[str, object]) -> dict[str, object]:
    """Zero-Lean heuristics for a census context whose flattened opens were lossy."""

    opens = [str(item) for item in cast(list[object], context.get("open_context", []))]
    scoped = [str(item) for item in cast(list[object], context.get("scoped_context", []))]
    namespaces = [str(item) for item in cast(list[object], context.get("namespace_context", []))]
    lowercase = [token for token in opens if token[:1].islower()]
    prefix_pairs = sorted(
        {
            (short, long)
            for short in opens
            for long in opens
            if short != long and long.startswith(short) and not long.startswith(short + ".")
        }
    )
    score = 3 * len(lowercase) + 2 * len(prefix_pairs) + len(scoped) + 0.1 * len(opens)
    return {
        "score": round(score, 3),
        "lowercase_open_tokens": lowercase,
        "prefix_pairs": [list(pair) for pair in prefix_pairs],
        "scoped_context": scoped,
        "namespace_context": namespaces,
        "open_count": len(opens),
    }


def _rank_key(salt: str, root_id: str) -> str:
    return hash_canonical({"salt": salt, "root_id": root_id})


def select_by_class(
    candidates: Sequence[tuple[str, str, float]],
    *,
    count: int,
    source_caps: Mapping[str, int],
    salt: str,
    exclude: set[str],
) -> list[str]:
    """Pick ``count`` root IDs by descending score, then salted hash, honouring per-source caps.

    ``candidates`` are ``(root_id, source, score)`` triples with score > 0. Caps bound each
    source; when a source is exhausted the next best candidates of other sources fill in.
    """

    ordered = sorted(
        (item for item in candidates if item[2] > 0 and item[0] not in exclude),
        key=lambda item: (-item[2], _rank_key(salt, item[0])),
    )
    chosen: list[str] = []
    per_source: Counter[str] = Counter()
    for root_id, source, _score in ordered:
        if len(chosen) >= count:
            break
        if per_source[source] >= int(source_caps.get(source, 0)):
            continue
        chosen.append(root_id)
        per_source[source] += 1
    if len(chosen) < count:
        for root_id, _source, _score in ordered:
            if len(chosen) >= count:
                break
            if root_id not in chosen:
                chosen.append(root_id)
    if len(chosen) < count:
        raise SprintRepairV3Error(f"only {len(chosen)} of {count} class roots are available")
    return chosen


def is_open_rendering_failure(detail: str) -> bool:
    """Classify a historical v2 Lean-invalid detail as an open-rendering (prelude) failure."""

    return any(pattern.search(detail) for pattern in _OPEN_FAILURE_PATTERNS)


# --------------------------------------------------------------------------------------------
# Canary freeze plus chained v3 shard configs (zero Lean, zero provider)
# --------------------------------------------------------------------------------------------


def _historical_shard_samples(plan: LoadedRepairPlanV3) -> list[dict[str, object]]:
    manifest = _object(plan.historical_shards_manifest_path)
    shards = manifest.get("shards")
    if not isinstance(shards, list) or not shards:
        raise SprintRepairV3Error("historical shards manifest lacks shard receipts")
    receipts: list[dict[str, object]] = []
    for item in shards:
        if not isinstance(item, dict):
            raise SprintRepairV3Error("historical shard receipt is malformed")
        sample_path = Path(str(item["sample_path"]))
        if hash_file(sample_path) != item.get("sample_sha256"):
            raise SprintRepairV3Error(f"frozen shard sample hash differs: {sample_path}")
        receipts.append(dict(item))
    return receipts


def _provider_defaults(plan: LoadedRepairPlanV3) -> dict[str, object]:
    return {
        "version": SPRINT_PILOT_VERSION,
        "status": "sprint_authorized",
        "authorized": True,
        "sprint_authority": str(plan.document.get("sprint_authority")),
        "repair_plan_path": str(plan.path.relative_to(plan.repo_root)),
        "repair_plan_sha256": plan.sha256,
        "base_config_path": str(plan.base_config_path.relative_to(plan.repo_root)),
        "base_config_sha256": hash_file(plan.base_config_path),
        "labeling_defaults_policy_path": str(plan.document["labeling_defaults_policy_path"]),
        "labeling_defaults_policy_sha256": str(plan.document["labeling_defaults_policy_sha256"]),
        "oracle_cache_version": "v3",
        "oracle_v3_gate_receipt_path": str(plan.oracle_v3_gate_receipt_path),
        "legacy_rejudge_authorized": False,
        "publication_authorized": False,
        "scale_10k_authorized": False,
        "scale_50k_authorized": False,
        "training_authorized": False,
    }


def freeze_sprint_canary_v3(plan: LoadedRepairPlanV3) -> dict[str, object]:
    """Freeze the 20-root defect-class canary sample, its provider config, and the chained v3
    provider configs for the frozen shards 2-10. Zero Lean, zero provider calls, replayable."""

    canary_root = Path(str(plan.canary["output_root"]))
    manifest_path = canary_root / "canary_manifest.json"
    if manifest_path.is_file():
        return _object(manifest_path)
    shard_receipts = _historical_shard_samples(plan)
    shard_sample_paths = [Path(str(item["sample_path"])) for item in shard_receipts]
    rows = _jsonl(plan.pool.output_root / "pool.jsonl")
    accepted, rejected, shapes = _screen_certified(
        plan.pool, rows, extra_exclusion_paths=shard_sample_paths
    )
    by_root: dict[str, dict[str, object]] = {}
    for source in _SOURCES:
        for row in accepted.get(source, []):
            by_root[str(row["root_id"])] = row
    goals: dict[str, str] = {}
    for root_id, row in by_root.items():
        result = _object(_result_path(plan.pool.output_root, row))
        goals[root_id] = str(cast(dict[str, object], result["certification"])["goal_v1"])
    salt = str(plan.canary["salt"])
    selection_mode = str(plan.canary.get("selection", "defect_classes"))
    dagger_chosen: list[str] = []
    context_chosen: list[str] = []
    if selection_mode == "natural_mix":
        # Stratified salted-hash draw from the unused screened pool at the shard source
        # proportions: the representative complement of the adversarial defect-class canary.
        natural_mix = cast(Mapping[str, int], plan.canary["natural_source_mix"])
        for source in _SOURCES:
            quota = int(natural_mix.get(source, 0))
            ranked = sorted(
                (root_id for root_id, row in by_root.items() if str(row["source"]) == source),
                key=lambda root_id: _rank_key(salt + ":natural", root_id),
            )
            if len(ranked) < quota:
                raise SprintRepairV3Error(f"unused pool lacks {quota} {source} roots")
            context_chosen.extend(ranked[:quota])
    else:
        dagger_candidates = [
            (root_id, str(row["source"]), float(dagger_count(goals[root_id])))
            for root_id, row in by_root.items()
        ]
        dagger_chosen = select_by_class(
            dagger_candidates,
            count=int(cast(int, plan.canary["dagger_roots"])),
            source_caps=cast(Mapping[str, int], plan.canary["dagger_source_caps"]),
            salt=salt + ":dagger",
            exclude=set(),
        )
        context_candidates = []
        for root_id, row in by_root.items():
            context = cast(Mapping[str, object], row["compile_context"])
            risk = context_risk(context)
            if not risk["namespace_context"] and not risk["scoped_context"]:
                continue
            context_candidates.append(
                (root_id, str(row["source"]), float(cast(float, risk["score"])))
            )
        context_chosen = select_by_class(
            context_candidates,
            count=int(cast(int, plan.canary["context_roots"])),
            source_caps=cast(Mapping[str, int], plan.canary["context_source_caps"]),
            salt=salt + ":context",
            exclude=set(dagger_chosen),
        )
    chosen_ids = [*dagger_chosen, *context_chosen]
    if len(set(chosen_ids)) != len(chosen_ids):
        raise SprintRepairV3Error("canary classes overlap")
    base_config = plan.base.config
    assert isinstance(base_config, SFT2AV52Config)
    fraction = base_config.mechanism_rotation.maximum_family_fraction_per_polarity
    sample_rows: list[dict[str, object]] = []
    for root_id in chosen_ids:
        pool_row = by_root[root_id]
        row = _replacement_row(pool_row, _result_path(plan.pool.output_root, pool_row))
        sample_rows.append(row)
    rotation, effective_fraction = _plan_shard_rotation(
        [(root_id, shapes[root_id][0]) for root_id in chosen_ids],
        salt=f"{salt}:structured",
        configured_fraction=fraction,
    )
    classes: dict[str, dict[str, object]] = {}
    for row in sample_rows:
        root_id = str(cast(dict[str, object], row["root"])["root_id"])
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
        row["canary_defect_class"] = (
            "natural_mix"
            if selection_mode == "natural_mix"
            else "dagger_heavy"
            if root_id in dagger_chosen
            else "context"
        )
        classes[root_id] = {
            "defect_class": row["canary_defect_class"],
            "dagger_count": dagger_count(goals[root_id]),
            "context_risk": context_risk(
                cast(Mapping[str, object], cast(dict[str, object], row["root"])["compile_context"])
            ),
            "source": str(cast(dict[str, object], row["root"])["source"]),
        }
    sample_rows.sort(
        key=lambda row: (
            str(
                cast(dict[str, object], cast(dict[str, object], row["root"])["compile_context"])[
                    "project_id"
                ]
            ),
            str(cast(dict[str, object], row["root"])["root_id"]),
        )
    )
    sample_path = canary_root / "certified_sample.jsonl"
    _atomic_exact(sample_path, _jsonl_bytes(sample_rows))
    source_mix = dict(
        sorted(
            Counter(
                str(cast(dict[str, object], row["root"])["source"]) for row in sample_rows
            ).items()
        )
    )
    shard_configs = freeze_sprint_v3_shard_configs(
        plan, canary_sample_path=sample_path, shard_receipts=shard_receipts
    )
    first_shard_config = cast(list[dict[str, object]], shard_configs["configs"])[0]
    ceilings = dict(cast(Mapping[str, object], plan.canary["ceilings"]))
    ceilings["maximum_roots"] = len(sample_rows)
    canary_document: dict[str, object] = {
        **_provider_defaults(plan),
        "sprint_role": "canary",
        "sample_path": str(sample_path),
        "sample_sha256": hash_file(sample_path),
        "expected_source_mix": source_mix,
        "completed_root_sample_paths": [
            *(str(path) for path in plan.pool.exclusion_sample_paths),
            *(str(path) for path in shard_sample_paths),
        ],
        "provider_output_root": str(plan.canary["provider_output_root"]),
        "tmux_session": str(plan.canary["tmux_session"]),
        "resource_task": str(plan.canary["resource_task"]),
        "maximum_root_workers": 1,
        "maximum_total_lean_workers": 1,
        "maximum_measured_rss_gib": SHARD_LEAN_RSS_GIB_PER_WORKER,
        "lean_worker_policy": "single_cooperative_worker_leaves_one_for_sft1_sft2b",
        "provider_concurrency": int(cast(int, plan.canary["provider_concurrency"])),
        "fallback_provider_concurrency": int(
            cast(int, plan.canary.get("fallback_provider_concurrency", 8))
        ),
        "kimi_audit_rows": int(cast(int, plan.canary["kimi_audit_rows"])),
        "controlled_stop_after_completed_roots": 1,
        "ceilings": ceilings,
        "shared_candidate_registry_path": str(plan.shards_v3["shared_candidate_registry_path"]),
        "next_shard_config_path": str(first_shard_config["provider_config_path"]),
        "canary_defect_classes": {
            "dagger_heavy": dagger_chosen,
            "context": context_chosen,
        },
        "pass_thresholds": {
            "minimum_accepted_slots": 56,
            "planned_slots": 80,
            "zero_copied_inaccessible_name_failures": True,
            "zero_context_prelude_failures": True,
            "maximum_genuine_lean_invalid_fraction": 0.25,
            "raw_lean_invalid_rate": "reported, nonblocking after attribution",
            "zero_accepted_self_pairs_or_duplicates": True,
            "maximum_infrastructure_failure_fraction": 0.02,
            "forced_resume_and_zero_call_replay": True,
        },
    }
    config_path = plan.repo_root / str(plan.canary["provider_config_path"])
    _atomic_replace_json(config_path, canary_document)
    load_provider_rehearsal_v52(config_path)
    manifest: dict[str, object] = {
        "version": CANARY_MANIFEST_VERSION,
        "repair_plan_sha256": plan.sha256,
        "sample_path": str(sample_path),
        "sample_sha256": hash_file(sample_path),
        "roots": len(sample_rows),
        "source_mix": source_mix,
        "defect_classes": classes,
        "selection": selection_mode,
        "dagger_roots": dagger_chosen,
        "context_roots": context_chosen,
        "configured_family_fraction": fraction,
        "effective_family_fraction": effective_fraction,
        "screen_rejections": dict(sorted(rejected.items())),
        "unused_screened_roots": len(by_root),
        "provider_config_path": str(config_path),
        "provider_config_sha256": hash_file(config_path),
        "shards_v3": shard_configs,
        "lean_requests_executed": 0,
        "provider_calls_executed": 0,
    }
    _atomic_exact(manifest_path, canonical_json_bytes(manifest) + b"\n")
    return manifest


def freeze_sprint_v3_shard_configs(
    plan: LoadedRepairPlanV3,
    *,
    canary_sample_path: Path,
    shard_receipts: Sequence[Mapping[str, object]] | None = None,
    regenerate: bool = False,
) -> dict[str, object]:
    """Write the chained v3 provider configs for the frozen shards (never shard 1).

    ``regenerate`` rewrites the configs and manifest from the plan when no shard run has
    started (a config is a replaceable launch document, not a run artifact)."""

    receipts = (
        list(shard_receipts) if shard_receipts is not None else _historical_shard_samples(plan)
    )
    shard_root = Path(str(plan.shards_v3["output_root"]))
    manifest_path = shard_root / "shards_v3_manifest.json"
    if manifest_path.is_file() and not regenerate:
        return _object(manifest_path)
    started_shards: dict[int, int] = {}
    if regenerate:
        # A sample-verification receipt under run/preflight is not a run; a durable root-state
        # journal, root outputs, or a detached stage directory is. A started shard keeps its
        # launch document and receives only the plan's override fields (its worker re-reads
        # the document on resume); unstarted shards are rewritten from the plan.
        for started in sorted(shard_root.glob("shard_*/run")):
            if any((started / name).exists() for name in ("root_state.jsonl", "roots", "detached")):
                index = int(started.parent.name.split("_")[1])
                started_shards[index] = _completed_root_count(started / "root_state.jsonl")
        if manifest_path.is_file():
            manifest_path.unlink()
    first = int(cast(int, plan.shards_v3["first_shard"]))
    last = int(cast(int, plan.shards_v3["last_shard"]))
    by_index = {int(cast(int, item["shard"])): item for item in receipts}
    config_paths = {
        index: shard_root / f"shard_{index:02d}" / "provider_config.json"
        for index in range(first, last + 1)
    }
    shard_one = by_index.get(1)
    if shard_one is None:
        raise SprintRepairV3Error("historical shards manifest lacks shard 1")
    completed_paths = [
        *(str(path) for path in plan.pool.exclusion_sample_paths),
        str(shard_one["sample_path"]),
        str(canary_sample_path),
    ]
    defaults = _provider_defaults(plan)
    ceilings_template = dict(cast(Mapping[str, object], plan.shards_v3["ceilings"]))
    deadline = cast(dict[str, object], plan.pool.document["shards"]).get("sprint_deadline_utc")
    configs: list[dict[str, object]] = []
    checkpoint_plan = plan.shards_v3.get("in_run_checkpoint")
    for index in range(last, first - 1, -1):
        receipt = by_index.get(index)
        if receipt is None:
            raise SprintRepairV3Error(f"historical shards manifest lacks shard {index}")
        roots = int(cast(int, receipt["roots"]))
        if index in started_shards:
            existing = _object(config_paths[index])
            existing["genuine_lean_invalid_blocking"] = bool(
                plan.shards_v3.get("genuine_lean_invalid_blocking", True)
            )
            if (
                isinstance(checkpoint_plan, Mapping)
                and int(cast(int, checkpoint_plan.get("shard", 0))) == index
            ):
                roots_at = int(cast(int, checkpoint_plan.get("roots", 0)))
                existing["in_run_checkpoint_roots"] = roots_at
                existing["controlled_stop_after_completed_roots"] = max(
                    1, roots_at - started_shards[index]
                )
            existing["override_applied_at"] = _now()
            existing["override_completed_roots_at_apply"] = started_shards[index]
            _atomic_replace_json(config_paths[index], existing)
            configs.append(
                {
                    "shard": index,
                    "provider_config_path": str(config_paths[index]),
                    "provider_config_sha256": hash_file(config_paths[index]),
                    "sample_path": str(receipt["sample_path"]),
                    "sample_sha256": str(receipt["sample_sha256"]),
                    "roots": roots,
                    "started_override": True,
                }
            )
            continue
        ceilings = dict(ceilings_template)
        ceilings["maximum_roots"] = roots
        next_path = config_paths.get(index + 1)
        document: dict[str, object] = {
            **defaults,
            "sprint_role": "shard",
            "shard_index": index,
            "shard_count": last,
            "frozen_shard_sample_origin": "sprint_shards_1k_v1",
            "sample_path": str(receipt["sample_path"]),
            "sample_sha256": str(receipt["sample_sha256"]),
            "expected_source_mix": dict(cast(Mapping[str, int], receipt["source_mix"])),
            "completed_root_sample_paths": completed_paths,
            "provider_output_root": str(config_paths[index].parent / "run"),
            "tmux_session": f"leanfaith-sft2a-sprint-v3-shard-{index:02d}",
            "resource_task": f"SFT2A-SPRINT-V3-SHARD-{index:02d}",
            "maximum_root_workers": 1,
            "maximum_total_lean_workers": 1,
            "maximum_measured_rss_gib": SHARD_LEAN_RSS_GIB_PER_WORKER,
            "lean_worker_policy": "single_cooperative_worker_leaves_one_for_sft1_sft2b",
            "provider_concurrency": int(cast(int, plan.shards_v3["provider_concurrency"])),
            "fallback_provider_concurrency": int(
                cast(int, plan.shards_v3["fallback_provider_concurrency"])
            ),
            "kimi_audit_fraction": float(cast(float, plan.shards_v3["kimi_audit_fraction"])),
            "kimi_audit_rows_maximum": int(cast(int, plan.shards_v3["kimi_audit_rows_maximum"])),
            "minimum_accepted_rows_per_minute": float(
                cast(float, plan.shards_v3["minimum_accepted_rows_per_minute"])
            ),
            "controlled_stop_after_completed_roots": 0,
            "ceilings": ceilings,
            "shared_candidate_registry_path": str(plan.shards_v3["shared_candidate_registry_path"]),
            "sprint_deadline_utc": deadline,
            "projection_blocking": False,
            "genuine_lean_invalid_blocking": bool(
                plan.shards_v3.get("genuine_lean_invalid_blocking", True)
            ),
            "next_shard_config_path": None if next_path is None else str(next_path),
        }
        if (
            isinstance(checkpoint_plan, Mapping)
            and int(cast(int, checkpoint_plan.get("shard", 0))) == index
        ):
            roots_at = int(cast(int, checkpoint_plan.get("roots", 0)))
            document["controlled_stop_after_completed_roots"] = roots_at
            document["in_run_checkpoint_roots"] = roots_at
        _atomic_replace_json(config_paths[index], document)
        configs.append(
            {
                "shard": index,
                "provider_config_path": str(config_paths[index]),
                "provider_config_sha256": hash_file(config_paths[index]),
                "sample_path": str(receipt["sample_path"]),
                "sample_sha256": str(receipt["sample_sha256"]),
                "roots": roots,
            }
        )
    configs.sort(key=lambda item: int(cast(int, item["shard"])))
    for item in configs:
        load_provider_rehearsal_v52(Path(str(item["provider_config_path"])))
    manifest: dict[str, object] = {
        "version": SHARDS_V3_MANIFEST_VERSION,
        "repair_plan_sha256": plan.sha256,
        "first_shard": first,
        "last_shard": last,
        "configs": configs,
        "lean_requests_executed": 0,
        "provider_calls_executed": 0,
    }
    _atomic_exact(manifest_path, canonical_json_bytes(manifest) + b"\n")
    return manifest


def _completed_root_count(state_path: Path) -> int:
    if not state_path.is_file():
        return 0
    completed: set[str] = set()
    for line in state_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if isinstance(record, dict) and record.get("phase") == "complete":
            completed.add(str(record.get("root_id")))
    return len(completed)


def regenerate_sprint_v3_shard_configs(plan: LoadedRepairPlanV3) -> dict[str, object]:
    """Rewrite the v3 shard configs from the plan (zero Lean, zero provider, no run started)."""

    canary_root = Path(str(plan.canary["output_root"]))
    manifest = _object(canary_root / "canary_manifest.json")
    return freeze_sprint_v3_shard_configs(
        plan, canary_sample_path=Path(str(manifest["sample_path"])), regenerate=True
    )


# --------------------------------------------------------------------------------------------
# Zero-provider repair gates over shard-1 roots (bounded Lean)
# --------------------------------------------------------------------------------------------


def _root_loaded(base: LoadedSFT2AConfig, row: Mapping[str, object]) -> LoadedSFT2AConfig:
    root = OneRootConfig.model_validate(row["root"])
    config = base.config.model_copy(update={"root": root})
    return replace(base, config=config, config_hash=hash_canonical(config.model_dump(mode="json")))


def _historical_attempts(run_root: Path) -> dict[str, list[dict[str, object]]]:
    by_root: dict[str, list[dict[str, object]]] = defaultdict(list)
    for manifest_path in sorted(run_root.glob("roots/*/manifest.json")):
        attempts_path = manifest_path.parent / "attempts/terminal_attempts.jsonl"
        if not attempts_path.is_file():
            continue
        for line in attempts_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if isinstance(record, dict):
                by_root[str(record.get("root_id"))].append(record)
    return by_root


def select_adversarial_roots(
    sample_rows: Sequence[Mapping[str, object]],
    attempts_by_root: Mapping[str, Sequence[Mapping[str, object]]],
    *,
    dagger_roots: int,
    context_roots: int,
) -> dict[str, list[str]]:
    """Deterministically pick the adversarial roots from a completed shard's artifacts:
    the most dagger-heavy references, then the roots with the most historical open-rendering
    failures, topped up with the richest scoped-context roots."""

    goals = {
        str(cast(Mapping[str, object], row["root"])["root_id"]): str(
            cast(Mapping[str, object], row["certified_reference"])["goal_v1"]
        )
        for row in sample_rows
    }
    contexts = {
        str(cast(Mapping[str, object], row["root"])["root_id"]): cast(
            Mapping[str, object], cast(Mapping[str, object], row["root"])["compile_context"]
        )
        for row in sample_rows
    }
    library_roots = [
        root_id
        for row in sample_rows
        for root_id in (str(cast(Mapping[str, object], row["root"])["root_id"]),)
        if str(cast(Mapping[str, object], row["root"])["source"]) != "compiler_data"
    ]
    dagger = sorted(
        (root_id for root_id in library_roots if dagger_count(goals[root_id]) > 0),
        key=lambda root_id: (-dagger_count(goals[root_id]), root_id),
    )[:dagger_roots]
    open_failures: Counter[str] = Counter()
    for root_id, attempts in attempts_by_root.items():
        for attempt in attempts:
            lean = attempt.get("lean")
            detail = str(lean.get("detail", "")) if isinstance(lean, Mapping) else ""
            if attempt.get("status") == "lean_invalid" and is_open_rendering_failure(detail):
                open_failures[root_id] += 1
    context: list[str] = []
    for root_id in sorted(open_failures, key=lambda item: (-open_failures[item], item)):
        if len(context) >= max(0, context_roots - 1):
            break
        if root_id in library_roots and root_id not in dagger:
            context.append(root_id)
    scoped_ranked = sorted(
        (
            root_id
            for root_id in library_roots
            if root_id not in dagger
            and root_id not in context
            and contexts[root_id].get("scoped_context")
        ),
        key=lambda root_id: (
            -len(cast(list[object], contexts[root_id].get("scoped_context", []))),
            root_id,
        ),
    )
    for root_id in scoped_ranked:
        if len(context) >= context_roots:
            break
        context.append(root_id)
    if len(context) < context_roots:
        risky = sorted(
            (
                root_id
                for root_id in library_roots
                if root_id not in dagger and root_id not in context
            ),
            key=lambda root_id: (
                -float(cast(float, context_risk(contexts[root_id])["score"])),
                root_id,
            ),
        )
        for root_id in risky:
            if len(context) >= context_roots:
                break
            context.append(root_id)
    if len(dagger) != dagger_roots or len(context) != context_roots:
        raise SprintRepairV3Error("adversarial selection could not fill both defect classes")
    return {"dagger_heavy": dagger, "context": context}


def _claim_one_worker(
    plan: LoadedRepairPlanV3, *, task: str, journal: Path, wait_seconds: float, poll_seconds: float
) -> dict[str, object]:
    deadline = time.monotonic() + wait_seconds
    waits = 0
    while True:
        try:
            claimed = claim_resources(
                task=task,
                lean_workers=1,
                lean_rss_gib=SHARD_LEAN_RSS_GIB_PER_WORKER,
                gpu=False,
                pid=os.getpid(),
                owner_session="sprint-repair-v3-gates",
                worktree=plan.repo_root,
            )
        except ReservationError as exc:
            if "cap exceeded" not in str(exc) and "already reserved" not in str(exc):
                raise
            if time.monotonic() >= deadline:
                raise SprintRepairV3Error(f"Lean capacity unavailable: {exc}") from exc
            if waits % 10 == 0:
                _append_stage(
                    journal,
                    {
                        "event": "waiting_for_lean_capacity",
                        "detail": str(exc),
                        "capacity": sprint_capacity_check(
                            lean_workers=1, lean_rss_gib=SHARD_LEAN_RSS_GIB_PER_WORKER
                        ),
                    },
                )
            waits += 1
            time.sleep(poll_seconds)
            continue
        return {
            "task": claimed.task,
            "lean_workers": 1,
            "lean_rss_gib": SHARD_LEAN_RSS_GIB_PER_WORKER,
            "waits": waits,
        }


def run_v3_repair_gates(
    plan: LoadedRepairPlanV3,
    *,
    wait_for_capacity_seconds: float = 12 * 3600.0,
    capacity_poll_seconds: float = 60.0,
) -> dict[str, object]:
    """Run both zero-provider repair gates under one cooperative Lean worker.

    Gate A (adversarial, 12 shard-1 roots): the v3 effective context preflights with zero
    diagnostics, the authoring view validates against the certified closed-Expr hash and
    canonical universe profile, and the view carries no inaccessible name.

    Gate B (historical open-only failures): every shard-1 Lean-invalid attempt classified as an
    open-rendering failure (no inaccessible name in the candidate) is re-elaborated from its
    cached candidate text through the v3 oracle; zero results may be attributed to the
    synthesized prelude, and genuine candidate-local errors stay invalid.
    """

    output_root = plan.repair_gates_output_root
    output_root.mkdir(parents=True, exist_ok=True)
    receipt_path = output_root / "repair_gates_receipt.json"
    if receipt_path.is_file():
        existing = _object(receipt_path)
        if existing.get("all_passed") is True and existing.get("repair_plan_sha256") == plan.sha256:
            return existing
    journal = output_root / "repair_gates_stage_journal.jsonl"
    _append_stage(journal, {"event": "gates_started", "repair_plan_sha256": plan.sha256})
    sample_rows = _jsonl(plan.historical_shard_sample_path)
    attempts_by_root = _historical_attempts(plan.historical_shard_run_root)
    adversarial = cast(Mapping[str, object], plan.document.get("adversarial", {}))
    selection = select_adversarial_roots(
        sample_rows,
        attempts_by_root,
        dagger_roots=int(cast(int, adversarial.get("dagger_roots", 6))),
        context_roots=int(cast(int, adversarial.get("context_roots", 6))),
    )
    rows_by_id = {
        str(cast(Mapping[str, object], row["root"])["root_id"]): row for row in sample_rows
    }
    # Gate B selection is fully determined by durable shard-1 artifacts.
    reelaboration_targets: list[dict[str, object]] = []
    for root_id, attempts in sorted(attempts_by_root.items()):
        if root_id not in rows_by_id:
            continue
        for attempt in attempts:
            lean = attempt.get("lean")
            signature = attempt.get("candidate_signature")
            detail = str(lean.get("detail", "")) if isinstance(lean, Mapping) else ""
            if (
                attempt.get("status") == "lean_invalid"
                and isinstance(signature, str)
                and INACCESSIBLE_NAME_MARK not in signature
                and is_open_rendering_failure(detail)
            ):
                reelaboration_targets.append(
                    {
                        "root_id": root_id,
                        "slot_id": attempt.get("slot_id"),
                        "attempt_id": attempt.get("attempt_id"),
                        "candidate_signature": signature,
                        "historical_detail": detail[:300],
                    }
                )
    _append_stage(
        journal,
        {
            "event": "selection_complete",
            "adversarial": selection,
            "reelaboration_targets": len(reelaboration_targets),
        },
    )
    claim = _claim_one_worker(
        plan,
        task=str(plan.document.get("repair_gates_resource_task", "SFT2A-SPRINT-V3-REPAIR-GATES")),
        journal=journal,
        wait_seconds=wait_for_capacity_seconds,
        poll_seconds=capacity_poll_seconds,
    )
    _append_stage(journal, {"event": "resource_claimed", "claim": claim})
    started = time.monotonic()
    adversarial_rows: list[dict[str, object]] = []
    reelaborated: list[dict[str, object]] = []
    pool = OraclePool(cache_version="v3", workers=1)
    try:
        ordered_ids = sorted(
            [*selection["dagger_heavy"], *selection["context"]],
            key=lambda root_id: (
                str(
                    cast(
                        Mapping[str, object],
                        cast(Mapping[str, object], rows_by_id[root_id]["root"])["compile_context"],
                    )["project_id"]
                ),
                root_id,
            ),
        )
        for root_id in ordered_ids:
            row = rows_by_id[root_id]
            root_loaded = _root_loaded(plan.base, row)
            oracle = PooledOracle(pool, root_loaded)
            reference = certified_reference_result_v52(dict(row))
            expected_hash = _closed_expr_hash(reference)
            expected_levels = _canonical_level_params(reference)
            certified = cast(Mapping[str, object], row["certified_reference"])
            effective = oracle.effective_context()
            combined = cast(Mapping[str, object], effective.record["combined_preflight"])
            view = oracle.authoring_view(
                root_loaded.config.root.declaration_name,
                expected_closed_expr_hash=expected_hash,
                expected_level_params=list(expected_levels),
            )
            validated = view.status == "validated"
            # A root whose view could not be re-elaborated to the certified identity falls back
            # to the raw signature plus fresh names in the prompt (the pre-Lean rejection still
            # guarantees no copied inaccessible name); it is telemetry, not a failure. A view
            # that exists must match the certified identity exactly and carry no dagger.
            checks = {
                "certified_identity_unchanged": certified.get("closed_expr_hash") == expected_hash,
                "authoring_view_identity_when_validated": (
                    not validated
                    or (
                        view.closed_expr_hash == expected_hash
                        and tuple(view.canonical_level_params) == tuple(expected_levels)
                    )
                ),
                "no_inaccessible_name_in_view": (
                    not validated
                    or (view.text is not None and INACCESSIBLE_NAME_MARK not in view.text)
                ),
                "zero_prelude_diagnostics": int(cast(int, combined["diagnostic_count"])) == 0,
            }
            adversarial_rows.append(
                {
                    "root_id": root_id,
                    "defect_class": (
                        "dagger_heavy" if root_id in selection["dagger_heavy"] else "context"
                    ),
                    "source": root_loaded.config.root.source,
                    "declaration_name": root_loaded.config.root.declaration_name,
                    "dagger_count": dagger_count(str(certified.get("goal_v1", ""))),
                    "raw_open_context": list(root_loaded.config.root.compile_context.open_context),
                    "raw_scoped_context": list(
                        root_loaded.config.root.compile_context.scoped_context
                    ),
                    "effective_scoped_validated": list(
                        cast(list[str], effective.record["scoped_validated"])
                    ),
                    "effective_scoped_dropped": [
                        str(cast(Mapping[str, object], item)["name"])
                        for item in cast(list[object], effective.record["scoped_dropped"])
                    ],
                    "effective_context_fingerprint": effective.fingerprint,
                    "prelude_diagnostics": int(cast(int, combined["diagnostic_count"])),
                    "authoring_view": view.to_dict(),
                    "checks": checks,
                    "passed": all(checks.values()),
                }
            )
            _append_stage(
                journal,
                {"event": "adversarial_root", "root_id": root_id, "passed": all(checks.values())},
            )
        targets_by_root: dict[str, list[dict[str, object]]] = defaultdict(list)
        for target in reelaboration_targets:
            targets_by_root[str(target["root_id"])].append(target)
        ordered_targets = sorted(
            targets_by_root,
            key=lambda root_id: (
                str(
                    cast(
                        Mapping[str, object],
                        cast(Mapping[str, object], rows_by_id[root_id]["root"])["compile_context"],
                    )["project_id"]
                ),
                root_id,
            ),
        )
        for root_id in ordered_targets:
            root_loaded = _root_loaded(plan.base, rows_by_id[root_id])
            oracle = PooledOracle(pool, root_loaded)
            for target in targets_by_root[root_id]:
                result = oracle.elaborate(
                    str(target["candidate_signature"]), endpoint_role="candidate"
                )
                reelaborated.append(
                    {
                        **target,
                        "v3_status": result.status,
                        "v3_attribution": result.attribution,
                        "v3_detail": result.detail[:300],
                        "v3_cache_key": result.cache_key,
                        "v3_cache_hit": result.cache_hit,
                        "v3_elapsed_ms": result.elapsed_ms,
                    }
                )
            _append_stage(
                journal,
                {
                    "event": "reelaboration_root",
                    "root_id": root_id,
                    "attempts": len(targets_by_root[root_id]),
                },
            )
    finally:
        pool.close()
        release_resources(task=str(claim["task"]))
        _append_stage(journal, {"event": "resource_released"})
    _atomic_replace_json(output_root / "adversarial_rows.json", {"rows": adversarial_rows})
    reelaboration_path = output_root / "reelaboration_rows.jsonl"
    reelaboration_path.write_bytes(_jsonl_bytes(reelaborated))
    prelude_attributed = sum(row["v3_attribution"] == "context_prelude" for row in reelaborated)
    valid_now = sum(row["v3_status"] == "valid" for row in reelaborated)
    candidate_local = sum(row["v3_attribution"] == "candidate_local" for row in reelaborated)
    error_classes: Counter[str] = Counter(
        " ".join(str(row["v3_detail"]).split())[:60]
        for row in reelaborated
        if row["v3_status"] == "invalid"
    )
    validated_views = sum(
        cast(Mapping[str, object], row["authoring_view"])["status"] == "validated"
        for row in adversarial_rows
    )
    minimum_fraction = float(
        cast(
            float,
            cast(Mapping[str, object], plan.document.get("adversarial", {})).get(
                "minimum_validated_view_fraction", 0.5
            ),
        )
    )
    checks = {
        "adversarial_all_passed": bool(adversarial_rows)
        and all(bool(row["passed"]) for row in adversarial_rows),
        "adversarial_twelve_roots": len(adversarial_rows) == 12,
        "authoring_views_validated_at_least_floor": (
            bool(adversarial_rows) and validated_views >= minimum_fraction * len(adversarial_rows)
        ),
        "zero_prelude_attributed_reelaborations": prelude_attributed == 0,
        "reelaboration_targets_present": len(reelaborated) > 0,
        "no_infrastructure_results": all(
            row["v3_status"] != "infrastructure" for row in reelaborated
        ),
    }
    receipt: dict[str, object] = {
        "version": REPAIR_GATES_VERSION,
        "repair_plan_sha256": plan.sha256,
        "base_config_hash": plan.base.config_hash,
        "method_version": ORACLE_METHOD_VERSION_V3,
        "cache_version": "v3",
        "elaborator_sha256": elaborator_sha256("v3"),
        "command_template_version": COMMAND_TEMPLATE_VERSION_V3,
        "effective_context_version": EFFECTIVE_CONTEXT_VERSION_V3,
        "authoring_view_version": AUTHORING_VIEW_VERSION_V3,
        "historical_shard_run_root": str(plan.historical_shard_run_root),
        "adversarial_selection": selection,
        "adversarial": {
            "roots": len(adversarial_rows),
            "passed": sum(bool(row["passed"]) for row in adversarial_rows),
            "authoring_views_validated": validated_views,
            "authoring_views_unavailable": len(adversarial_rows) - validated_views,
            "minimum_validated_view_fraction": minimum_fraction,
            "unavailable_details": [
                {
                    "root_id": row["root_id"],
                    "detail": str(cast(Mapping[str, object], row["authoring_view"])["detail"])[
                        :300
                    ],
                }
                for row in adversarial_rows
                if cast(Mapping[str, object], row["authoring_view"])["status"] != "validated"
            ],
            "authoring_view_profiles": dict(
                Counter(
                    str(cast(Mapping[str, object], row["authoring_view"])["profile"])
                    for row in adversarial_rows
                )
            ),
            "rows_path": str(output_root / "adversarial_rows.json"),
        },
        "reelaboration": {
            "targets": len(reelaborated),
            "valid_now": valid_now,
            "invalid_candidate_local": candidate_local,
            "invalid_prelude_attributed": prelude_attributed,
            "invalid_copied_inaccessible_name": sum(
                row["v3_attribution"] == "copied_inaccessible_name" for row in reelaborated
            ),
            "cache_hits": sum(bool(row["v3_cache_hit"]) for row in reelaborated),
            "error_classes": dict(error_classes.most_common(12)),
            "rows_path": str(reelaboration_path),
        },
        "checks": checks,
        "all_passed": all(checks.values()),
        "resource_claim": claim,
        "oracle_pool": dict(pool.stats),
        "elapsed_seconds": time.monotonic() - started,
        "provider_calls_executed": 0,
        "completed_at": _now(),
    }
    _atomic_replace_json(receipt_path, receipt)
    _append_stage(journal, {"event": "gates_finished", "all_passed": receipt["all_passed"]})
    if not bool(receipt["all_passed"]):
        raise SprintRepairV3Error(f"v3 repair gates failed: {checks}")
    return receipt


__all__ = [
    "CANARY_MANIFEST_VERSION",
    "REPAIR_GATES_VERSION",
    "REPAIR_PLAN_VERSION",
    "SHARDS_V3_MANIFEST_VERSION",
    "LoadedRepairPlanV3",
    "SprintRepairV3Error",
    "context_risk",
    "dagger_count",
    "freeze_sprint_canary_v3",
    "freeze_sprint_v3_shard_configs",
    "is_open_rendering_failure",
    "load_repair_plan_v3",
    "regenerate_sprint_v3_shard_configs",
    "run_v3_repair_gates",
    "select_adversarial_roots",
    "select_by_class",
]
