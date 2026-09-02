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
import shutil
import sys
import time
from collections.abc import Mapping, Sequence
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
    loaded: LoadedConfig[SprintConfig], run_ids: Sequence[str] = SOURCE_RUNS
) -> list[dict[str, Any]]:
    """Certified N25 roots from the source runs, deduplicated by the reference
    closed-Expr hash and ordered by a stable hash of that identity."""

    best: dict[str, dict[str, Any]] = {}
    for run_id in run_ids:
        paths = RunPaths(Path(loaded.config.output.staging_root), run_id)
        for record in read_retained(paths.retained):
            if record["operation_id"] != "N25_TOGGLE_EQ_NE_PROOF_V1":
                continue
            sidecar = record["sidecar"]
            direction = str((sidecar.get("site") or {}).get("detail", ""))
            if direction not in {"eq_to_ne", "ne_to_eq"}:
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


def write_census(loaded: LoadedConfig[SprintConfig], out: Path) -> dict[str, Any]:
    roots = eligible_roots(loaded)
    payload = {
        "schema_version": 1,
        "operation_id": SQUARE_OPERATION,
        "source_runs": list(SOURCE_RUNS),
        "count": len(roots),
        "by_direction": _count_by(roots, "direction"),
        "roots_sha256": hash_canonical([item["name"] for item in roots]),
        "roots": roots,
    }
    write_atomic(out, canonical_json_bytes(payload) + b"\n")
    return {k: v for k, v in payload.items() if k != "roots"}


# ------------------------------------------------------------------ runner


def process_body(names: Sequence[str]) -> str:
    literals = ", ".join(lean_string_literal(name) for name in names)
    return f"run_meta do\n  LeanFaith.SFT1.Sprint.processSquares #[{literals}]"


def render_body(names: Sequence[str], scope: str) -> str:
    literals = ", ".join(lean_string_literal(name) for name in names)
    lines = [
        "run_meta do",
        f"  let squares ← LeanFaith.SFT1.Sprint.rebuildSquares #[{literals}]",
        "  LeanFaith.SFT1.Sprint.emitSquareReport squares",
    ]
    fields = {"p": "p", "c": "c", "p_prime": "pPrime", "c_prime": "cPrime"}
    for index in range(len(names)):
        for endpoint, field in fields.items():
            lines.append(
                f"  LeanFaith.GoalV1.emitClosedProp {json.dumps(f'{index}.{endpoint}')} "
                f"{json.dumps(scope)} {json.dumps(ENDPOINT_ORIGIN[endpoint])} "
                f"(squares[{index}]!).{field}"
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
    ) -> None:
        self.base = SprintRunner(repo_root, loaded, run_id=run_id, owner_session=owner_session)
        self.repo_root = repo_root
        self.loaded = loaded
        self.config = loaded.config
        self.run_id = run_id
        self.roots = list(roots)
        self.max_roots = max_roots
        self.paths = self.base.paths
        self.journal = self.base.journal
        self.cache = self.base.cache
        self.done: dict[str, str] = {}
        self.retained = 0
        self.counts: dict[str, int] = {}
        self.lean_roots = 0
        self.cache_roots = 0
        self.started = time.monotonic()
        self.batches = 0
        self.statements: dict[str, str] = {}

    # ---------------------------------------------------------------- state

    def load_state(self) -> None:
        for record in self.journal.read():
            if record.get("kind") == "square_terminal":
                name = str(record["root"])
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
        inventory_dir = Path(self.config.inventory.root) / self.config.project.project_revision
        for row in load_inventory(inventory_dir / "inventory.jsonl"):
            self.statements.setdefault(str(row["name"]), str(row["statement"]))

    def cache_key(self, name: str, reference_alpha_hash: str) -> str:
        return SemanticCache.op_key(
            reference_alpha_hash=reference_alpha_hash,
            operation_id=SQUARE_OPERATION,
            engine_semantic_version=self.base.identity.semantic_version,
            lean_version=self.base.pins.lean_version,
            project_revision=self.base.pins.project_revision,
            import_options_fingerprint=self.base.identity.import_options_fingerprint,
            name=name,
        )

    def square_root_key(self, name: str) -> str:
        return hash_canonical(
            {
                "kind": "square_root",
                "cache_schema": 2,
                "operation_id": SQUARE_OPERATION,
                "name": name,
                "engine_semantic_version": self.base.identity.semantic_version,
                "project_revision": self.base.pins.project_revision,
                "lean_version": self.base.pins.lean_version,
                "import_options_fingerprint": self.base.identity.import_options_fingerprint,
            }
        )

    # ---------------------------------------------------------------- run

    def run(self, *, require_zero_lean: bool = False) -> dict[str, Any]:
        self.base.verify_pins()
        self.load_state()
        self.write_run_manifest()
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

    def write_run_manifest(self) -> None:
        if self.paths.run_manifest.is_file():
            return
        manifest = {
            "schema_version": 1,
            "sprint_id": self.config.sprint_id,
            "run_id": self.run_id,
            "operation_id": SQUARE_OPERATION,
            "started_at": utc_now(),
            "config_semantic_hash": self.loaded.config_hash,
            "engine": self.base.identity.to_dict(),
            "project": self.base.pins.to_dict(),
            "implementation_commit": _git(self.repo_root, "rev-parse", "HEAD"),
            "implementation_dirty": bool(_git(self.repo_root, "status", "--porcelain")),
            "max_roots": self.max_roots,
            "roots_sha256": hash_canonical([str(item["name"]) for item in self.roots]),
            "root_count": len(self.roots),
            "argv": sys.argv,
        }
        write_atomic(self.paths.run_manifest, canonical_json_bytes(manifest) + b"\n")

    # ---------------------------------------------------------------- cache

    def try_cache(self, root: Mapping[str, Any]) -> bool:
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
            code=engine_module.command_text(self.base.context, process_body(names)),
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
                    self.cache.put_root(self.square_root_key(name), record)
                self.finalize(name, record, source="lean", root=root)
                continue
            violation = self.screen_payload(payload)
            if violation is not None:
                payload = dict(payload)
                payload["status"] = "rejected"
                payload["reason"] = violation
                record = self.cache_record(name, payload, None, result.request_hash)
                self.cache.put_root(self.square_root_key(name), record)
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
                session_body=render_body(names, scope),
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
            self.cache.put_root(self.square_root_key(name), record)
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
            "operation_id": SQUARE_OPERATION,
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
        self, name: str, record: Mapping[str, Any], root: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        render = cast(dict[str, Any], record["render"])
        evidence = cast(dict[str, Any], record["evidence"])
        direction = str(record["direction"])
        root_id = self.base.root_id(name)
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
                    "operation_id": SQUARE_OPERATION,
                    "row_kind": kind,
                    "reference_expr_hash": ref_hash,
                    "candidate_expr_hash": cand_hash,
                },
            )
            check = cast(dict[str, Any], evidence[evidence_key])
            if label:
                row_evidence: dict[str, Any] = {
                    "label": True,
                    "equivalence_proof": {"goal": "Iff reference candidate", "check": check},
                    "source_proof": evidence["source_proof"],
                    "candidate_truth": "proved_equivalent_to_reference",
                }
            else:
                row_evidence = {
                    "label": False,
                    "refutation": {"goal": "Not (Iff reference candidate)", "check": check},
                    "source_proof": evidence["source_proof"],
                    "source_proof_check": evidence["source_proof_check"],
                    "candidate_truth": "proved" if cand_ep == "p" else "refuted",
                    "reference_truth": "refuted",
                }
            row_evidence["square"] = evidence
            evidence_hash = hash_canonical(row_evidence)
            row_hash = hash_canonical(
                {
                    "root_id": root_id,
                    "operation_id": SQUARE_OPERATION,
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
                "operation_id": SQUARE_OPERATION,
                "mechanism": engine_module.mechanism_of(SQUARE_OPERATION),
                "row_kind": kind,
                "row_schema": ROW_SCHEMA,
                "label": label,
                "orientation": "square_fixed",
                "core_family": f"square_{'eq' if direction == 'eq_to_ne' else 'ne'}",
                "core_cell": kind,
                "square": {
                    "direction": direction,
                    "t_p": evidence["t_p"],
                    "t_c": evidence["t_c"],
                    "reference_endpoint": ref_ep,
                    "candidate_endpoint": cand_ep,
                    "source_run": root.get("source_run"),
                    "source_pair_id": root.get("source_pair_id"),
                },
                "site": {"kind": "square", "detail": direction},
                "evidence": row_evidence,
                "evidence_hash": evidence_hash,
                "candidate_truth": row_evidence["candidate_truth"],
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
                "engine": self.base.identity.to_dict(),
                "cache_key": self.cache_key(name, str(record["alpha"]["p"])),
                "lean_request_hashes": {
                    "process": record.get("process_request_hash"),
                    "render": render.get("request_hash"),
                },
                "level_params": record.get("level_params"),
                "implementation_commit": self.base.implementation_commit,
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
                    "operation_id": SQUARE_OPERATION,
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
            "operation_id": SQUARE_OPERATION,
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


def build_square_view(
    repo_root: Path,
    loaded: LoadedConfig[SprintConfig],
    *,
    run_id: str,
    label: str = "core_v3_square",
) -> dict[str, Any]:
    from leanfaith.sft1.sprint import shortcut

    config = loaded.config
    paths = RunPaths(Path(config.output.staging_root), run_id)
    records = read_retained(paths.retained)
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
    outcome = deduplicate(screened)
    conflicting_rows = sum(
        1 for record in screened if str(record["unordered_pair_key"]) in set(outcome.conflict_keys)
    )
    kept_records = cast(list[dict[str, Any]], outcome.kept)
    by_root: dict[str, list[dict[str, Any]]] = {}
    for record in kept_records:
        by_root.setdefault(str(record["sidecar"]["root_id"]), []).append(record)
    complete_roots = {root for root, items in by_root.items() if len(items) == 4}
    incomplete = len(by_root) - len(complete_roots)
    ordered_roots = sorted(complete_roots, key=lambda root: hash_canonical([SQUARE_SALT, root]))
    kept: list[dict[str, Any]] = []
    for root in ordered_roots:
        kind_order = {kind: index for index, (kind, *_rest) in enumerate(ROW_KINDS)}
        kept.extend(
            sorted(by_root[root], key=lambda item: kind_order[str(item["sidecar"]["row_kind"])])
        )
    out = Path(config.output.staging_root) / "compacted" / label
    if out.exists():
        raise SquareError(f"{out} already exists; square views are additive and immutable")
    out.mkdir(parents=True)
    provenance = derive_provenance(
        kept, repo_root=repo_root, cache_root=Path(config.output.staging_root) / "cache"
    )
    if not provenance["consistent"]:
        raise SquareError("provenance inconsistent: " + "; ".join(provenance["issues"]))
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
        "source_runs": [run_id],
        "compacted_at": utc_now(),
        "finalized": True,
        "input_records": len(records),
        "screen_rejections": rejections,
        "duplicates_removed": outcome.duplicate_count,
        "conflicting_classes_rejected": outcome.conflict_count,
        "conflicting_rows_rejected": conflicting_rows,
        "deduplicated_records": len(outcome.kept),
        "incomplete_squares_dropped": incomplete,
        "view_dropped": len(outcome.kept) - len(kept),
        "retained_rows": len(kept),
        "labels": {
            "positive": sum(1 for item in kept if item["label"]),
            "negative": sum(1 for item in kept if not item["label"]),
        },
        "operations": _count_by(kept, "operation_id"),
        "mechanisms": _count_by(kept, "mechanism"),
        "row_kinds": _count_by([item["sidecar"] for item in kept], "row_kind"),
        "families": _count_by([item["sidecar"] for item in kept], "core_family"),
        "roots": len(complete_roots),
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
    }
    write_atomic(out / "manifest.json", canonical_json_bytes(manifest) + b"\n")
    serialized = shortcut.load_serialized_view(out)
    screens = shortcut.run_screens_v3(serialized)
    control = shortcut.permutation_control(serialized)
    write_atomic(out / "permutation_control.json", canonical_json_bytes(control) + b"\n")
    screen_by_name = {str(s["name"]): s for s in cast(list[dict[str, Any]], screens["screens"])}
    unchecked = 0
    marginal_ok = True
    positives_ref: dict[str, int] = {}
    negatives_ref: dict[str, int] = {}
    positives_cand: dict[str, int] = {}
    negatives_cand: dict[str, int] = {}
    for item in serialized:
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
        "rows_are_exactly_reference_candidate_label": all(
            set(item["row"]) == {"reference", "candidate", "label"} for item in serialized
        ),
        "four_rows_per_root": all(len(by_root[root]) == 4 for root in complete_roots)
        and len(kept) == 4 * len(complete_roots),
        "zero_incomplete_squares": incomplete == 0,
        "labels_balanced": manifest["labels"]["positive"] == manifest["labels"]["negative"],
        "identical_marginals_across_labels": marginal_ok,
        "all_rows_kernel_and_meta_checked_at_generation": unchecked == 0,
        "zero_duplicates": outcome.duplicate_count == 0,
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
        "source_runs": [run_id],
        "evaluated_on": "serialized_shards",
        "rows": len(serialized),
        "labels": manifest["labels"],
        "roots": len(complete_roots),
        "families": manifest["families"],
        "row_kinds": manifest["row_kinds"],
        "negative_mechanisms_informational": {
            "note": "all square negatives derive from certified N25 pairs (Eq→Ne and Ne→Eq)",
            "by_family": manifest["families"],
        },
        "unchecked_rows": unchecked,
        "shortcut": screens,
        "permutation_control": {k: v for k, v in control.items() if k != "per_seed"},
        "proof_check_time": "original_generation",
        "replay_semantics": "journal_and_cache_replay_of_stored_terminals_no_fresh_kernel_replay",
        "checks": checks,
        "passed": all(checks.values()),
    }
    write_atomic(out / "release_report.json", canonical_json_bytes(report) + b"\n")
    manifest["artifact_status"] = (
        "square_release_high_confidence"
        if report["passed"]
        else "candidate_square_release_gate_failed"
    )
    write_atomic(out / "manifest.json", canonical_json_bytes(manifest) + b"\n")
    return report


# ------------------------------------------------------------------ fixtures


def run_square_fixtures(
    repo_root: Path, loaded: LoadedConfig[SprintConfig], fixtures: Sequence[Mapping[str, str]]
) -> dict[str, Any]:
    roots = [
        {"name": item["root"], "direction": "fixture", "reference_expr_hash": item["root"]}
        for item in fixtures
    ]
    pins = SprintRunner(repo_root, loaded, run_id="square-fixtures").identity
    run_id = f"square-fixtures-{pins.source_sha256[:12]}"
    # Fixture gates always start fresh: they must exercise the engine, never resume a journal.
    stale_run_dir = RunPaths(Path(loaded.config.output.staging_root), run_id).run_dir
    if stale_run_dir.exists():
        shutil.rmtree(stale_run_dir)
    runner = SquareRunner(repo_root, loaded, run_id=run_id, roots=roots)
    runner.run()
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
        "status": runner.write_status(final=True),
    }
    write_atomic(
        runner.paths.run_dir / "fixtures_report.json", canonical_json_bytes(report) + b"\n"
    )
    return report


# ------------------------------------------------------------------ CLI

SQUARE_FIXTURES: tuple[dict[str, str], ...] = (
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
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("census", "fixtures", "run", "replay", "build", "status")
    )
    parser.add_argument("--repo-root", type=Path, default=find_repo_root(Path.cwd()))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--run-id", default="square_full")
    parser.add_argument("--max-roots", type=int)
    parser.add_argument("--label", default="core_v3_square")
    parser.add_argument("--owner-session", default="claude-sft1-square")
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()
    loaded = load_sprint_config(repo_root, args.config.resolve() if args.config else None)
    staging = Path(loaded.config.output.staging_root)
    if args.command == "census":
        print(json.dumps(write_census(loaded, staging / "targets" / "square_n25.json"), indent=1))
        return 0
    if args.command == "fixtures":
        report = run_square_fixtures(repo_root, loaded, SQUARE_FIXTURES)
        print(json.dumps({k: v for k, v in report.items() if k != "status"}, indent=1))
        return 0 if report["passed"] else 1
    if args.command in {"run", "replay"}:
        census_path = staging / "targets" / "square_n25.json"
        census = read_json_object(census_path)
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
    report = build_square_view(repo_root, loaded, run_id=args.run_id, label=args.label)
    print(
        json.dumps(
            {k: v for k, v in report.items() if k not in {"shortcut"}}, ensure_ascii=False, indent=1
        )
    )
    print(json.dumps(report["shortcut"]["screens"], indent=1))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
