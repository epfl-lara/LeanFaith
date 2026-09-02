"""Certificate-closure squares around certified N25 negatives (``core_v3_square``).

For each certified negative ``P ≁ C`` the engine applies the matching relation
symmetry ``T`` to both endpoints, checks the exact typed diamond
``T(N(P)) = N(T(P))``, and constructs direct Meta- and kernel-checked evidence
for ``P ↔ P'``, ``C ↔ C'``, the transported proof of ``P'``, the refutation of
``C'``, ``¬(C ↔ P)`` and ``¬(P' ↔ C')``.  Four grouped rows are emitted per
accepted root with identical reference and candidate marginals across labels:

1. positive ``reference=P', candidate=P``
2. positive ``reference=C, candidate=C'``
3. negative ``reference=C, candidate=P``
4. negative ``reference=P', candidate=C'``
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.config.loading import LoadedConfig
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
from leanfaith.schemas.ids import PAIR_PREFIX, make_id
from leanfaith.sft1.sprint import engine as engine_module
from leanfaith.sft1.sprint.engine import cacheable_status, lean_string_literal, parse_evidence_lines
from leanfaith.sft1.sprint.inventory import load_inventory
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
}


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
SOURCE_RUNS = ("tenk", "v2_ne", "v2_lt")


class SquareError(RuntimeError):
    """Fail-closed square error."""


# ------------------------------------------------------------------ eligibility


def eligible_roots(
    loaded: LoadedConfig[SprintConfig],
    run_ids: Sequence[str] = SOURCE_RUNS,
    negative_operation: str = "N25_TOGGLE_EQ_NE_PROOF_V1",
) -> list[dict[str, Any]]:
    """Certified roots of one negative operation from the source runs, deduplicated by
    the reference closed-Expr hash and ordered by a stable hash of that identity."""

    allowed_directions = (
        {"eq_to_ne", "ne_to_eq"} if negative_operation.startswith("N25") else {"Nat", "Int"}
    )
    best: dict[str, dict[str, Any]] = {}
    for run_id in run_ids:
        paths = RunPaths(Path(loaded.config.output.staging_root), run_id)
        for record in read_retained(paths.retained):
            if record["operation_id"] != negative_operation:
                continue
            sidecar = record["sidecar"]
            direction = str((sidecar.get("site") or {}).get("detail", ""))
            if direction not in allowed_directions:
                continue
            key = str(sidecar["repr"]["reference"]["provenance"]["expr_hash"])
            entry = {
                "name": str(sidecar["root_name"]),
                "direction": direction,
                "reference_expr_hash": key,
                "source_run": run_id,
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
) -> dict[str, Any]:
    negative = SQUARE_OPERATIONS[operation_id]["negative"]
    if negative in INVENTORY_NEGATIVES:
        if repo_root is None:
            raise SquareError("inventory census needs the repository root")
        roots = inventory_roots(repo_root, loaded)
    else:
        roots = eligible_roots(loaded, negative_operation=negative)
    payload = {
        "schema_version": 1,
        "operation_id": operation_id,
        "negative_operation": negative,
        "source_runs": list(SOURCE_RUNS),
        "count": len(roots),
        "by_direction": _count_by(roots, "direction"),
        "roots_sha256": hash_canonical([item["name"] for item in roots]),
        "roots": roots,
    }
    write_atomic(out, canonical_json_bytes(payload) + b"\n")
    return {k: v for k, v in payload.items() if k != "roots"}


# ------------------------------------------------------------------ runner


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
        if missing and len(batch) > 1:
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


def reconcile_square_alpha(record: Mapping[str, Any], raw_dir: Path) -> dict[str, Any]:
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
    files = sorted(raw_dir.glob(f"{request_hash}.*.json"))
    result["raw_files"] = len(files)
    if not files:
        result["reason"] = "raw_render_response_missing"
        return result
    rebuilds: list[dict[str, str]] = []
    for path in files:
        raw = read_json_object(path)
        if raw.get("request_hash") != request_hash:
            result["reason"] = "raw_file_request_hash_mismatch"
            return result
        code = str(cast(dict[str, Any], raw.get("request") or {}).get("code") or "")
        call = _REBUILD_CALL.search(code)
        if call is None:
            result["reason"] = "rebuild_call_not_found"
            return result
        names = [json.loads(literal) for literal in _STRING_LITERAL.findall(call.group(1))]
        if chunk_index >= len(names) or names[chunk_index] != record.get("root"):
            result["reason"] = "chunk_name_mismatch"
            return result
        entries = _square_rebuild_entries(raw)
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
    paths: RunPaths, name: str, *, raw_dir: Path, operation_id: str
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
    for path in sorted(raw_dir.glob(f"{hashes.get('process', '')}.*.json")):
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
                runner.paths, name, raw_dir=raw_dir, operation_id=runner.operation_id
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
        reconciliation = reconcile_square_alpha(record, raw_dir)
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


def square_key(sidecar: Mapping[str, Any]) -> str:
    """One square = one root under one square operation."""
    return f"{sidecar['root_id']}|{sidecar.get('operation_id', SQUARE_OPERATION)}"


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
        manifests.append({"file": name, "squares": len(lines), "sha256": sha256_hex(payload)})
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
            records.extend(load_square_retained(paths))
            source_retained_paths.append(str(paths.retained.relative_to(staging)))
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
    selection = select_squares(screened, outcome.conflict_keys, preferred_operations)
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
    distinct_roots = {str(record["sidecar"]["root_id"]) for record in kept}
    conservation = {
        "screened_rows": len(screened),
        "kept_rows": len(kept),
        "duplicate_square_rows_dropped": duplicate_rows,
        "degenerate_square_rows_dropped": degenerate_rows,
        "superseded_square_rows_dropped": superseded_rows,
        "holds": len(screened) == len(kept) + duplicate_rows + degenerate_rows + superseded_rows,
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
        "input_records": len(records),
        "screen_rejections": rejections,
        "duplicate_rows_seen": outcome.duplicate_count,
        "duplicate_squares_dropped": len(selection.duplicate_squares),
        "degenerate_squares_dropped": incomplete,
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
}


# ------------------------------------------------------------------ inspection
SQUARE_ROW_ORDER: dict[str, int] = {kind[0]: index for index, kind in enumerate(ROW_KINDS)}


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
    """Every retained square row grouped by root, four rows per root, with check flags."""
    by_root: dict[str, list[Mapping[str, Any]]] = {}
    for record in records:
        by_root.setdefault(str(record["sidecar"]["root_name"]), []).append(record)
    lines = [
        "# core_v3_square inspection",
        "",
        f"- roots: {len(by_root)}",
        f"- rows: {len(records)}",
        "",
    ]
    for name in sorted(by_root):
        recs = sorted(by_root[name], key=lambda r: SQUARE_ROW_ORDER[str(r["sidecar"]["row_kind"])])
        first = cast(dict[str, Any], recs[0]["sidecar"])
        square = cast(dict[str, Any], first.get("square", {}))
        evidence = cast(
            dict[str, Any], cast(dict[str, Any], first.get("evidence", {})).get("square", {})
        )
        flags = []
        for key, value in evidence.items():
            flag = _check_flag(value)
            if flag is not None:
                kind = value.get("kind") if isinstance(value, dict) else None
                flags.append(f"{key}:{flag}" + (f"({kind})" if kind else ""))
        lines += [
            f"## {name}",
            "",
            f"- module: `{first.get('module')}`",
            f"- direction: {square.get('direction')} "
            f"(T_P={square.get('t_p')}, T_C={square.get('t_c')})",
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("census", "fixtures", "run", "replay", "build", "status", "inspect")
    )
    parser.add_argument("--repo-root", type=Path, default=find_repo_root(Path.cwd()))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--run-id", default="square_full")
    parser.add_argument("--run-ids", help="comma-separated run ids for build (default: --run-id)")
    parser.add_argument(
        "--prefer-operations",
        help="comma-separated square operations that supersede other squares of the same root",
    )
    parser.add_argument("--operation", default=SQUARE_OPERATION, choices=sorted(SQUARE_OPERATIONS))
    parser.add_argument("--max-roots", type=int)
    parser.add_argument("--label", default="core_v3_square")
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
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()
    loaded = load_sprint_config(repo_root, args.config.resolve() if args.config else None)
    staging = Path(loaded.config.output.staging_root)
    if args.command == "census":
        report = write_census(
            loaded, census_path_for(staging, args.operation), args.operation, repo_root=repo_root
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
        census = read_json_object(census_path_for(staging, args.operation))
        roots = cast(list[dict[str, Any]], census["roots"])
        max_roots = args.max_roots
        manifest_path = RunPaths(staging, args.run_id).run_manifest
        if args.command == "replay" and manifest_path.is_file():
            recorded = read_json_object(manifest_path)
            if max_roots is None and isinstance(recorded.get("max_roots"), int):
                max_roots = int(recorded["max_roots"])
        runner = SquareRunner(
            repo_root,
            loaded,
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
    report = build_square_view(
        repo_root,
        loaded,
        run_ids=[x.strip() for x in (args.run_ids or args.run_id).split(",") if x.strip()],
        label=args.label,
        regenerate=not args.no_regenerate,
        supersedes=args.supersedes,
        require_identical_rows=args.require_identical_rows,
        preferred_operations=[
            x.strip() for x in (args.prefer_operations or "").split(",") if x.strip()
        ],
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
