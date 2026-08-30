"""Resumable SFT2A one-root/four-slot orchestration and blinded Lemex audit."""

from __future__ import annotations

import fcntl
import json
import math
import os
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.representations.views import signature_near_dup_hash
from leanfaith.sft2a.config import LoadedSFT2AConfig
from leanfaith.sft2a.layout import run_paths
from leanfaith.sft2a.lean_oracle import (
    ORACLE_METHOD_VERSION,
    SignatureOracle,
    SignatureOracleResult,
)
from leanfaith.sft2a.models import (
    CoreRow,
    JudgeOutput,
    ProposerOutput,
    SFT2AOpusConfig,
    SFT2AProductionConfig,
    SlotConfig,
)
from leanfaith.sft2a.prompts import (
    accepted_verdict_for,
    prompt_hash,
    render_blinded_judge_prompt,
    render_proposer_prompt,
)
from leanfaith.sft2a.providers import (
    ProviderCallResult,
    claude_judge_provider,
    lemex_audit_provider,
    proposer_provider,
)


class OneRootPipelineError(RuntimeError):
    """The bounded run, replay, journal, or output contract failed."""


class StructuredProvider(Protocol):
    def call(self, *, prompt: str, input_ids: Sequence[str]) -> ProviderCallResult: ...


class PropositionOracle(Protocol):
    def elaborate(
        self,
        signature: str,
        *,
        endpoint_role: Literal["reference", "candidate"],
    ) -> SignatureOracleResult: ...

    def close(self) -> None: ...


class CrossRootCandidateRegistry(Protocol):
    def claim(self, *, raw_signature: str, rendered_goal: str, owner: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class OneRootResult:
    output_root: Path
    manifest: dict[str, object]
    replayed: bool


@dataclass(frozen=True, slots=True)
class AuditResult:
    output_root: Path
    manifest: dict[str, object]
    replayed: bool


def _canonical_jsonl(rows: Sequence[Mapping[str, object]]) -> bytes:
    return b"".join(canonical_json_bytes(dict(row)) + b"\n" for row in rows)


def _atomic(path: Path, payload: bytes) -> bool:
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise OneRootPipelineError(f"immutable SFT2A output conflict: {path}")
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return False


def _load_object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OneRootPipelineError(f"invalid JSON artifact {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise OneRootPipelineError(f"JSON artifact is not an object: {path}")
    return value


def _append_event(path: Path, event: Mapping[str, object]) -> None:
    event_payload = dict(event)
    event_id = "sft2a-event:" + hash_canonical(event_payload)
    row = {"event_id": event_id, **event_payload}
    line = canonical_json_bytes(row) + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.seek(0)
            existing = handle.read().splitlines()
            for raw in existing:
                try:
                    observed = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise OneRootPipelineError(
                        f"attempt journal contains invalid JSON: {exc}"
                    ) from exc
                if isinstance(observed, dict) and observed.get("event_id") == event_id:
                    if raw + b"\n" != line:
                        raise OneRootPipelineError("attempt journal event ID conflict")
                    return
            handle.seek(0, os.SEEK_END)
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _artifact_receipts(output_root: Path, relative_paths: Sequence[str]) -> dict[str, object]:
    receipts: dict[str, object] = {}
    for relative in relative_paths:
        path = output_root / relative
        if not path.is_file() or path.is_symlink():
            raise OneRootPipelineError(f"required output artifact is missing or unsafe: {path}")
        receipts[relative] = {
            "sha256": hash_file(path),
            "bytes": path.stat().st_size,
            "rows": sum(1 for _ in path.open("rb")) if path.suffix == ".jsonl" else None,
        }
    return receipts


def _validate_complete_replay(output_root: Path, manifest: Mapping[str, object]) -> None:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise OneRootPipelineError("one-root manifest lacks artifact receipts")
    for relative, receipt in artifacts.items():
        if not isinstance(relative, str) or not isinstance(receipt, dict):
            raise OneRootPipelineError("one-root artifact receipt is malformed")
        path = output_root / relative
        if not path.is_file() or path.is_symlink() or hash_file(path) != receipt.get("sha256"):
            raise OneRootPipelineError(f"one-root replay artifact differs: {relative}")


def _gold_blocklist(loaded: LoadedSFT2AConfig) -> tuple[set[str], set[str]]:
    policy = loaded.config.gold_screen
    path = loaded.repo_root / policy.path
    if hash_file(path) != policy.sha256:
        raise OneRootPipelineError("gold blocklist differs from the frozen SFT2A input")
    document = _load_object(path)
    group_keys = document.get("group_keys")
    near_dup_hashes = document.get("near_dup_hashes")
    if (
        not isinstance(group_keys, list)
        or not isinstance(near_dup_hashes, list)
        or any(not isinstance(value, str) for value in group_keys)
        or any(not isinstance(value, str) for value in near_dup_hashes)
        or len(group_keys) != policy.group_key_count
        or len(near_dup_hashes) != policy.near_dup_hash_count
    ):
        raise OneRootPipelineError("gold blocklist has a malformed or unexpected population")
    return set(group_keys), set(near_dup_hashes)


def _gold_signature_hit(signature: str, blocked_hashes: set[str]) -> tuple[bool, str]:
    digest = signature_near_dup_hash(signature)
    return digest in blocked_hashes, digest


def _feedback(status: str, detail: str) -> str:
    detail = " ".join(detail.split())[:600]
    return f"status={status}; detail={detail}"


def _attempt_record(
    *,
    root_id: str,
    slot: SlotConfig,
    attempt_number: int,
    status: str,
    proposer_call: ProviderCallResult | None,
    proposer: ProposerOutput | None,
    lean: SignatureOracleResult | None,
    judge_call: ProviderCallResult | None,
    judge: JudgeOutput | None,
    detail: str,
) -> dict[str, object]:
    candidate_signature = None if proposer is None else proposer.candidate_signature
    candidate_hash = None if lean is None else lean.signature_sha256
    attempt_id = "sft2a-attempt:" + hash_canonical(
        {
            "root_id": root_id,
            "slot_id": slot.slot_id,
            "attempt_number": attempt_number,
            "candidate_signature": candidate_signature,
        }
    )
    return {
        "attempt_id": attempt_id,
        "root_id": root_id,
        "slot_id": slot.slot_id,
        "requested_polarity": slot.requested_polarity,
        "attempt_number": attempt_number,
        "status": status,
        "detail": detail,
        "candidate_signature": candidate_signature,
        "candidate_signature_sha256": candidate_hash,
        "proposer": None if proposer is None else proposer.model_dump(mode="json"),
        "proposer_call_key": None if proposer_call is None else proposer_call.call_key,
        "proposer_prompt_hash": None,
        "lean": (
            None
            if lean is None
            else {
                "status": lean.status,
                "cache_key": lean.cache_key,
                "cache_hit": lean.cache_hit,
                "lean_status": lean.lean_status,
                "request_hash": lean.request_hash,
                "elapsed_ms": lean.elapsed_ms,
                "raw_response_path": lean.raw_response_path,
                "detail": lean.detail,
            }
        ),
        "judge": None if judge is None else judge.model_dump(mode="json"),
        "judge_call_key": None if judge_call is None else judge_call.call_key,
        "judge_blinded": judge is not None,
        "label_basis": "proposer_intent+single_judge" if status == "accepted" else None,
    }


def run_one_root(
    loaded: LoadedSFT2AConfig,
    *,
    proposer: StructuredProvider | None = None,
    claude_judge: StructuredProvider | None = None,
    oracle: PropositionOracle | None = None,
    output_root: Path | None = None,
    enforce_expected_reference_goal: bool = True,
    enforce_smoke_ceilings: bool = True,
    cross_root_registry: CrossRootCandidateRegistry | None = None,
) -> OneRootResult:
    """Run or replay exactly the configured root and four independent candidate slots."""

    output_root = output_root or run_paths(loaded).one_root
    manifest_path = output_root / "manifest.json"
    if manifest_path.exists():
        replay_manifest = _load_object(manifest_path)
        if replay_manifest.get("config_hash") != loaded.config_hash:
            raise OneRootPipelineError("one-root manifest config hash differs")
        _validate_complete_replay(output_root, replay_manifest)
        return OneRootResult(output_root=output_root, manifest=replay_manifest, replayed=True)

    proposer_client = proposer or proposer_provider(loaded)
    judge_client = claude_judge or claude_judge_provider(loaded)
    own_oracle = oracle is None
    proposition_oracle = oracle or SignatureOracle(loaded)
    journal_path = output_root / "attempt_journal.jsonl"
    attempts: list[dict[str, object]] = []
    core_rows: list[dict[str, object]] = []
    sidecars: list[dict[str, object]] = []
    invalid_rows: list[dict[str, object]] = []
    unknown_rows: list[dict[str, object]] = []
    rejected_rows: list[dict[str, object]] = []
    contamination_rows: list[dict[str, object]] = []
    all_provider_calls: list[ProviderCallResult] = []
    all_lean_results: list[SignatureOracleResult] = []
    seen_candidate_signatures: set[str] = set()
    retry_slots: set[str] = set()
    blocked_groups, blocked_hashes = _gold_blocklist(loaded)

    try:
        source_group_key = f"{loaded.config.root.source}::{loaded.config.root.declaration_name}"
        raw_reference_hit, raw_reference_hash = _gold_signature_hit(
            loaded.config.root.reference_signature, blocked_hashes
        )
        if source_group_key in blocked_groups or raw_reference_hit:
            raise OneRootPipelineError("one-root reference matches the frozen gold blocklist")
        reference = proposition_oracle.elaborate(
            loaded.config.root.reference_signature,
            endpoint_role="reference",
        )
        all_lean_results.append(reference)
        if reference.status != "valid" or reference.goal_v1 is None or reference.sidecar is None:
            raise OneRootPipelineError(
                f"one-root reference failed proof-free elaboration: {reference.detail}"
            )
        if (
            enforce_expected_reference_goal
            and reference.goal_v1 != loaded.config.root.expected_reference_goal_v1
        ):
            raise OneRootPipelineError(
                "one-root reference REPR differs from the frozen expected goal:\n"
                f"observed={reference.goal_v1!r}"
            )
        rendered_reference_hit, rendered_reference_hash = _gold_signature_hit(
            reference.goal_v1, blocked_hashes
        )
        if rendered_reference_hit:
            raise OneRootPipelineError("one-root rendered reference matches the gold blocklist")

        for slot in loaded.config.slots:
            feedback: str | None = None
            accepted = False
            for attempt_number in range(1, slot.max_attempts + 1):
                if attempt_number > 1:
                    retry_slots.add(slot.slot_id)
                proposer_prompt = render_proposer_prompt(
                    loaded,
                    slot=slot,
                    attempt_number=attempt_number,
                    attempt_feedback=feedback,
                )
                proposer_call = proposer_client.call(
                    prompt=proposer_prompt,
                    input_ids=(
                        loaded.config.root.root_id,
                        slot.slot_id,
                        f"attempt:{attempt_number}",
                    ),
                )
                all_provider_calls.append(proposer_call)
                try:
                    proposal = ProposerOutput.model_validate(proposer_call.structured)
                except Exception as exc:
                    detail = f"proposer_schema_rejected:{type(exc).__name__}:{exc}"
                    record = _attempt_record(
                        root_id=loaded.config.root.root_id,
                        slot=slot,
                        attempt_number=attempt_number,
                        status="proposer_rejected",
                        proposer_call=proposer_call,
                        proposer=None,
                        lean=None,
                        judge_call=None,
                        judge=None,
                        detail=detail,
                    )
                    record["proposer_prompt_hash"] = prompt_hash(proposer_prompt)
                    attempts.append(record)
                    invalid_rows.append(record)
                    _append_event(journal_path, record)
                    feedback = _feedback("proposer_rejected", detail)
                    continue
                if proposal.requested_polarity != slot.requested_polarity:
                    detail = "proposer requested_polarity differs from the slot"
                    record = _attempt_record(
                        root_id=loaded.config.root.root_id,
                        slot=slot,
                        attempt_number=attempt_number,
                        status="proposer_rejected",
                        proposer_call=proposer_call,
                        proposer=proposal,
                        lean=None,
                        judge_call=None,
                        judge=None,
                        detail=detail,
                    )
                    record["proposer_prompt_hash"] = prompt_hash(proposer_prompt)
                    attempts.append(record)
                    invalid_rows.append(record)
                    _append_event(journal_path, record)
                    feedback = _feedback("proposer_rejected", detail)
                    continue
                candidate_key = " ".join(proposal.candidate_signature.split())
                reference_key = " ".join(loaded.config.root.reference_signature.split())
                if candidate_key == reference_key or candidate_key in seen_candidate_signatures:
                    detail = "candidate is a reference copy or a prior candidate duplicate"
                    record = _attempt_record(
                        root_id=loaded.config.root.root_id,
                        slot=slot,
                        attempt_number=attempt_number,
                        status="duplicate_rejected",
                        proposer_call=proposer_call,
                        proposer=proposal,
                        lean=None,
                        judge_call=None,
                        judge=None,
                        detail=detail,
                    )
                    record["proposer_prompt_hash"] = prompt_hash(proposer_prompt)
                    attempts.append(record)
                    invalid_rows.append(record)
                    _append_event(journal_path, record)
                    feedback = _feedback("duplicate_rejected", detail)
                    continue
                seen_candidate_signatures.add(candidate_key)

                raw_candidate_hit, raw_candidate_hash = _gold_signature_hit(
                    proposal.candidate_signature, blocked_hashes
                )
                if raw_candidate_hit:
                    detail = "raw candidate matches the frozen gold near-duplicate screen"
                    record = _attempt_record(
                        root_id=loaded.config.root.root_id,
                        slot=slot,
                        attempt_number=attempt_number,
                        status="gold_contamination",
                        proposer_call=proposer_call,
                        proposer=proposal,
                        lean=None,
                        judge_call=None,
                        judge=None,
                        detail=detail,
                    )
                    record["proposer_prompt_hash"] = prompt_hash(proposer_prompt)
                    record["gold_screen"] = {
                        "field": "raw_candidate",
                        "signature_near_dup_hash": raw_candidate_hash,
                    }
                    attempts.append(record)
                    contamination_rows.append(record)
                    _append_event(journal_path, record)
                    feedback = _feedback("gold_contamination", detail)
                    continue

                lean = proposition_oracle.elaborate(
                    proposal.candidate_signature,
                    endpoint_role="candidate",
                )
                all_lean_results.append(lean)
                if lean.status != "valid" or lean.goal_v1 is None or lean.sidecar is None:
                    status = "lean_invalid" if lean.status == "invalid" else "lean_infrastructure"
                    record = _attempt_record(
                        root_id=loaded.config.root.root_id,
                        slot=slot,
                        attempt_number=attempt_number,
                        status=status,
                        proposer_call=proposer_call,
                        proposer=proposal,
                        lean=lean,
                        judge_call=None,
                        judge=None,
                        detail=lean.detail,
                    )
                    record["proposer_prompt_hash"] = prompt_hash(proposer_prompt)
                    attempts.append(record)
                    invalid_rows.append(record)
                    _append_event(journal_path, record)
                    if lean.status == "infrastructure":
                        raise OneRootPipelineError(
                            f"Lean infrastructure failed for {slot.slot_id}: {lean.detail}"
                        )
                    feedback = _feedback(status, lean.detail)
                    continue

                rendered_candidate_hit, rendered_candidate_hash = _gold_signature_hit(
                    lean.goal_v1, blocked_hashes
                )
                if rendered_candidate_hit:
                    detail = "rendered candidate matches the frozen gold near-duplicate screen"
                    record = _attempt_record(
                        root_id=loaded.config.root.root_id,
                        slot=slot,
                        attempt_number=attempt_number,
                        status="gold_contamination",
                        proposer_call=proposer_call,
                        proposer=proposal,
                        lean=lean,
                        judge_call=None,
                        judge=None,
                        detail=detail,
                    )
                    record["proposer_prompt_hash"] = prompt_hash(proposer_prompt)
                    record["gold_screen"] = {
                        "field": "rendered_candidate",
                        "signature_near_dup_hash": rendered_candidate_hash,
                    }
                    attempts.append(record)
                    contamination_rows.append(record)
                    _append_event(journal_path, record)
                    feedback = _feedback("gold_contamination", detail)
                    continue

                claim_owner = (
                    f"{loaded.config.root.root_id}:{slot.slot_id}:attempt:{attempt_number}"
                )
                if cross_root_registry is not None and not cross_root_registry.claim(
                    raw_signature=proposal.candidate_signature,
                    rendered_goal=lean.goal_v1,
                    owner=claim_owner,
                ):
                    detail = "candidate duplicates a prior valid candidate from another root"
                    record = _attempt_record(
                        root_id=loaded.config.root.root_id,
                        slot=slot,
                        attempt_number=attempt_number,
                        status="cross_root_duplicate",
                        proposer_call=proposer_call,
                        proposer=proposal,
                        lean=lean,
                        judge_call=None,
                        judge=None,
                        detail=detail,
                    )
                    record["proposer_prompt_hash"] = prompt_hash(proposer_prompt)
                    attempts.append(record)
                    invalid_rows.append(record)
                    _append_event(journal_path, record)
                    feedback = _feedback("cross_root_duplicate", detail)
                    continue

                judge_prompt = render_blinded_judge_prompt(
                    loaded,
                    statement_a=reference.goal_v1,
                    statement_b=lean.goal_v1,
                )
                judge_call = judge_client.call(
                    prompt=judge_prompt,
                    input_ids=(
                        loaded.config.root.root_id,
                        lean.signature_sha256,
                        "blinded_claude_judge",
                    ),
                )
                all_provider_calls.append(judge_call)
                judgment = JudgeOutput.model_validate(judge_call.structured)
                expected = accepted_verdict_for(slot.requested_polarity)
                if judgment.verdict != expected:
                    status = "judge_unknown" if judgment.verdict == "unknown" else "judge_disagreed"
                    record = _attempt_record(
                        root_id=loaded.config.root.root_id,
                        slot=slot,
                        attempt_number=attempt_number,
                        status=status,
                        proposer_call=proposer_call,
                        proposer=proposal,
                        lean=lean,
                        judge_call=judge_call,
                        judge=judgment,
                        detail=judgment.rationale,
                    )
                    record["proposer_prompt_hash"] = prompt_hash(proposer_prompt)
                    record["judge_prompt_hash"] = prompt_hash(judge_prompt)
                    attempts.append(record)
                    (unknown_rows if judgment.verdict == "unknown" else rejected_rows).append(
                        record
                    )
                    _append_event(journal_path, record)
                    feedback = _feedback(status, judgment.rationale)
                    continue

                row_id = "sft2a-new:" + hash_canonical(
                    {
                        "root_id": loaded.config.root.root_id,
                        "slot_id": slot.slot_id,
                        "candidate_signature_sha256": lean.signature_sha256,
                        "requested_polarity": slot.requested_polarity,
                    }
                )
                core = CoreRow(
                    reference=reference.goal_v1,
                    candidate=lean.goal_v1,
                    label=slot.requested_polarity == "preserving",
                ).model_dump(mode="json")
                record = _attempt_record(
                    root_id=loaded.config.root.root_id,
                    slot=slot,
                    attempt_number=attempt_number,
                    status="accepted",
                    proposer_call=proposer_call,
                    proposer=proposal,
                    lean=lean,
                    judge_call=judge_call,
                    judge=judgment,
                    detail="proposer intent and blinded Claude judgment agree",
                )
                record["proposer_prompt_hash"] = prompt_hash(proposer_prompt)
                record["judge_prompt_hash"] = prompt_hash(judge_prompt)
                record["row_id"] = row_id
                attempts.append(record)
                core_rows.append(core)
                sidecars.append(
                    {
                        "row_id": row_id,
                        "root_id": loaded.config.root.root_id,
                        "slot_id": slot.slot_id,
                        "requested_polarity": slot.requested_polarity,
                        "generation_attempt": attempt_number,
                        "label_provenance": "proposer_intent+single_judge",
                        "proposer": proposal.model_dump(mode="json"),
                        "proposer_provider": loaded.config.proposer.model_dump(mode="json"),
                        "proposer_call_key": proposer_call.call_key,
                        "proposer_prompt_hash": prompt_hash(proposer_prompt),
                        "compilation_cache_key": lean.cache_key,
                        "reference_repr": reference.sidecar,
                        "candidate_repr": lean.sidecar,
                        "claude_judge": judgment.model_dump(mode="json"),
                        "claude_provider": loaded.config.claude_judge.model_dump(mode="json"),
                        "claude_call_key": judge_call.call_key,
                        "judge_prompt_hash": prompt_hash(judge_prompt),
                        "raw_reference_signature": loaded.config.root.reference_signature,
                        "raw_candidate_signature": proposal.candidate_signature,
                    }
                )
                _append_event(journal_path, record)
                accepted = True
                break
            if not accepted:
                unresolved = {
                    "root_id": loaded.config.root.root_id,
                    "slot_id": slot.slot_id,
                    "requested_polarity": slot.requested_polarity,
                    "attempts_exhausted": slot.max_attempts,
                    "training_eligible": False,
                }
                unknown_rows.append(unresolved)
                _append_event(journal_path, {"status": "slot_unresolved", **unresolved})
    finally:
        if own_oracle:
            proposition_oracle.close()

    artifact_payloads: dict[str, bytes] = {
        "new_core/core.jsonl": _canonical_jsonl(core_rows),
        "new_core/sidecar.jsonl": _canonical_jsonl(sidecars),
        "invalid/attempts.jsonl": _canonical_jsonl(invalid_rows),
        "unknown/rows.jsonl": _canonical_jsonl(unknown_rows),
        "rejected/rows.jsonl": _canonical_jsonl(rejected_rows),
        "contamination/rows.jsonl": _canonical_jsonl(contamination_rows),
        "attempts/terminal_attempts.jsonl": _canonical_jsonl(attempts),
    }
    for relative, payload in artifact_payloads.items():
        _atomic(output_root / relative, payload)
    artifact_paths = (*artifact_payloads, "attempt_journal.jsonl")
    receipts = _artifact_receipts(output_root, artifact_paths)
    proposer_calls = [
        call
        for call in all_provider_calls
        if call.provider_id == loaded.config.proposer.provider_id
    ]
    claude_calls = [
        call
        for call in all_provider_calls
        if call.provider_id == loaded.config.claude_judge.provider_id
    ]
    candidate_lean = all_lean_results[1:]
    executed_lean = [result for result in candidate_lean if not result.cache_hit]
    lean_seconds = sum(result.elapsed_ms for result in executed_lean) / 1000

    def valid_lean_attempt(row: Mapping[str, object]) -> bool:
        lean = row.get("lean")
        return isinstance(lean, dict) and lean.get("status") == "valid"

    if isinstance(loaded.config, SFT2AOpusConfig) and enforce_smoke_ceilings:
        ceiling = loaded.config.smoke_ceilings
        if isinstance(loaded.config, SFT2AProductionConfig) and any(
            call.cost_usd is None for call in claude_calls
        ):
            raise OneRootPipelineError("production smoke Opus call lacks reported cost")
        opus_spend = sum(call.cost_usd or 0.0 for call in claude_calls if not call.cache_hit)
        if len(all_provider_calls) > ceiling.maximum_provider_calls:
            raise OneRootPipelineError("one-root provider-call ceiling exceeded")
        if len(proposer_calls) > ceiling.maximum_proposer_calls:
            raise OneRootPipelineError("one-root proposer-call ceiling exceeded")
        if len(claude_calls) > ceiling.maximum_opus_calls:
            raise OneRootPipelineError("one-root Opus-call ceiling exceeded")
        if opus_spend > ceiling.maximum_reported_opus_spend_usd:
            raise OneRootPipelineError("one-root reported Opus spend ceiling exceeded")
        if not retry_slots and not isinstance(loaded.config, SFT2AProductionConfig):
            if any(not call.cache_hit for call in proposer_calls):
                raise OneRootPipelineError("Opus smoke unexpectedly executed a proposer call")
            if any(not result.cache_hit for result in all_lean_results):
                raise OneRootPipelineError("Opus smoke unexpectedly executed a Lean request")

    final_manifest: dict[str, object] = {
        "version": (
            "leanfaith_sft2a_one_root_production_defaults_manifest_v1"
            if isinstance(loaded.config, SFT2AProductionConfig)
            else (
                "leanfaith_sft2a_one_root_opus5_manifest_v1"
                if isinstance(loaded.config, SFT2AOpusConfig)
                else "leanfaith_sft2a_one_root_manifest_v1"
            )
        ),
        "config_hash": loaded.config_hash,
        "config_file_sha256": hash_file(loaded.path),
        "root_id": loaded.config.root.root_id,
        "root_count": 1,
        "slot_count": 4,
        "counts": {
            "accepted": len(core_rows),
            "accepted_positive": sum(bool(row["label"]) for row in core_rows),
            "accepted_negative": sum(not bool(row["label"]) for row in core_rows),
            "invalid_attempts": len(invalid_rows),
            "unknown_rows": len(unknown_rows),
            "judge_disagreements": len(rejected_rows),
            "gold_contamination": len(contamination_rows),
            "cross_root_duplicates": sum(
                row.get("status") == "cross_root_duplicate" for row in invalid_rows
            ),
            "retry_slots": len(retry_slots),
            "attempts": len(attempts),
        },
        "quality": {
            "accepted_siblings_preserved": len(core_rows),
            "slot_attempt_cap": 3,
            "retry_slots": sorted(retry_slots),
            "all_valid_candidates_independently_judged": all(
                row.get("judge_blinded") is True for row in attempts if valid_lean_attempt(row)
            ),
        },
        "lean": {
            "method_version": ORACLE_METHOD_VERSION,
            "oracle_source_sha256": hash_file(
                loaded.repo_root / "src/leanfaith/sft2a/lean_oracle.py"
            ),
            "candidate_requests": len(candidate_lean),
            "candidate_cache_hits": sum(result.cache_hit for result in candidate_lean),
            "candidate_executed": len(executed_lean),
            "candidate_elapsed_seconds": lean_seconds,
            "candidate_rows_per_second": (
                None if lean_seconds == 0 else len(executed_lean) / lean_seconds
            ),
            "status_counts": dict(Counter(result.status for result in candidate_lean)),
        },
        "llm": {
            "proposer_calls": len(proposer_calls),
            "proposer_cache_hits": sum(call.cache_hit for call in proposer_calls),
            "claude_calls": len(claude_calls),
            "claude_cache_hits": sum(call.cache_hit for call in claude_calls),
            "nominal_cost_usd": sum(call.cost_usd or 0.0 for call in all_provider_calls),
            "executed_cost_usd": sum(
                call.cost_usd or 0.0 for call in all_provider_calls if not call.cache_hit
            ),
            "latency_seconds": sum(call.elapsed_seconds for call in all_provider_calls),
            "usage": [
                {
                    "provider_id": call.provider_id,
                    "call_key": call.call_key,
                    "cache_hit": call.cache_hit,
                    "usage": call.usage,
                    "cost_usd": call.cost_usd,
                }
                for call in all_provider_calls
            ],
            "provider_pins": {
                "proposer": loaded.config.proposer.model_dump(mode="json"),
                "claude_judge": loaded.config.claude_judge.model_dump(mode="json"),
            },
            "cost_limitations": {
                "codex": "unavailable",
                "claude": "reported_by_cli",
            },
        },
        "repr": loaded.config.repr.model_dump(mode="json"),
        "gold_screen": {
            **loaded.config.gold_screen.model_dump(mode="json"),
            "source_group_key": source_group_key,
            "raw_reference_hash": raw_reference_hash,
            "rendered_reference_hash": rendered_reference_hash,
            "matches": len(contamination_rows),
        },
        "artifacts": receipts,
        "attempt_journal": str(journal_path),
        "scale_50k_started": False,
        "published": False,
        "pilot_started": False,
    }
    if isinstance(loaded.config, SFT2AOpusConfig) and enforce_smoke_ceilings:
        final_manifest["execution_ceilings"] = loaded.config.smoke_ceilings.model_dump(mode="json")
        final_manifest["server_model_pin_limit"] = (
            "Claude CLI alias 'opus' is pinned locally but resolves to a floating server model"
        )
    _atomic(manifest_path, canonical_json_bytes(final_manifest) + b"\n")
    return OneRootResult(output_root=output_root, manifest=final_manifest, replayed=False)


def verify_one_root_replay(loaded: LoadedSFT2AConfig) -> dict[str, object]:
    """Prove a completed rerun is byte-stable and creates no provider/Lean artifacts."""

    paths = run_paths(loaded)
    staging = paths.shared_cache_root
    output_root = paths.one_root
    manifest_path = output_root / "manifest.json"
    if not manifest_path.is_file():
        raise OneRootPipelineError("one-root manifest must exist before replay verification")

    def snapshot(
        root: Path,
        *,
        exclude: frozenset[str] = frozenset(),
        exclude_prefixes: tuple[str, ...] = (),
    ) -> dict[str, str]:
        if not root.exists():
            return {}
        return {
            str(path.relative_to(root)): hash_file(path)
            for path in sorted(root.rglob("*"))
            if path.is_file()
            and not path.is_symlink()
            and str(path.relative_to(root)) not in exclude
            and not any(
                str(path.relative_to(root)).startswith(prefix + "/") for prefix in exclude_prefixes
            )
        }

    before = {
        "provider_calls": snapshot(
            staging / "provider_calls",
            exclude_prefixes=(loaded.config.lemex_auditor.provider_id,),
        ),
        "lean_cache": snapshot(staging / "lean_cache"),
        "lean_raw_responses": snapshot(staging / "lean_raw_responses"),
        "one_root": snapshot(
            output_root,
            exclude=frozenset({"reproducibility_receipt.json"}),
            exclude_prefixes=(
                "audit_lemex_v1",
                "comparison_fable_opus_v1",
                "post_audit_release_v1",
            ),
        ),
    }
    replay = run_one_root(loaded)
    after = {
        "provider_calls": snapshot(
            staging / "provider_calls",
            exclude_prefixes=(loaded.config.lemex_auditor.provider_id,),
        ),
        "lean_cache": snapshot(staging / "lean_cache"),
        "lean_raw_responses": snapshot(staging / "lean_raw_responses"),
        "one_root": snapshot(
            output_root,
            exclude=frozenset({"reproducibility_receipt.json"}),
            exclude_prefixes=(
                "audit_lemex_v1",
                "comparison_fable_opus_v1",
                "post_audit_release_v1",
            ),
        ),
    }
    if not replay.replayed or before != after:
        raise OneRootPipelineError("one-root replay created, removed, or changed durable artifacts")
    receipt = {
        "version": "leanfaith_sft2a_one_root_replay_receipt_v1",
        "config_hash": loaded.config_hash,
        "manifest_sha256": hash_file(manifest_path),
        "artifact_snapshot_hash": hash_canonical(before),
        "provider_calls_executed": 0,
        "lean_requests_executed": 0,
        "duplicate_outputs_created": 0,
        "reproducible": True,
    }
    _atomic(output_root / "reproducibility_receipt.json", canonical_json_bytes(receipt) + b"\n")
    return receipt


def _stratified_audit_indices(
    sidecars: Sequence[Mapping[str, object]], fraction: float
) -> list[int]:
    strata: dict[tuple[str, str], list[tuple[str, int]]] = {}
    for index, row in enumerate(sidecars):
        polarity = str(row["requested_polarity"])
        judge = row.get("claude_judge")
        if not isinstance(judge, dict):
            raise OneRootPipelineError("new-core sidecar lacks Claude judgment")
        verdict = str(judge.get("verdict"))
        rank = hash_canonical({"row_id": row["row_id"], "audit": "sft2a_lemex_10pct_v1"})
        strata.setdefault((polarity, verdict), []).append((rank, index))
    selected: list[int] = []
    for ranked in strata.values():
        ranked.sort()
        count = max(1, math.ceil(len(ranked) * fraction))
        selected.extend(index for _rank, index in ranked[:count])
    return sorted(selected)


def run_lemex_audit(
    loaded: LoadedSFT2AConfig,
    *,
    auditor: StructuredProvider | None = None,
) -> AuditResult:
    """Run the blinded stratified 10% audit only after reproducible one-root output."""

    paths = run_paths(loaded)
    one_root = paths.one_root
    replay_receipt = one_root / "reproducibility_receipt.json"
    if not replay_receipt.is_file() or _load_object(replay_receipt).get("reproducible") is not True:
        raise OneRootPipelineError("Lemex audit requires the reproducible one-root receipt")
    audit_root = paths.audit
    manifest_path = audit_root / "manifest.json"
    if manifest_path.exists():
        replay_manifest = _load_object(manifest_path)
        if replay_manifest.get("config_hash") != loaded.config_hash:
            raise OneRootPipelineError("Lemex audit manifest config hash differs")
        artifacts = replay_manifest.get("artifacts")
        if not isinstance(artifacts, dict):
            raise OneRootPipelineError("Lemex audit manifest lacks artifact receipts")
        for relative, receipt in artifacts.items():
            if not isinstance(relative, str) or not isinstance(receipt, dict):
                raise OneRootPipelineError("Lemex audit artifact receipt is malformed")
            path = audit_root / relative
            if not path.is_file() or hash_file(path) != receipt.get("sha256"):
                raise OneRootPipelineError(f"Lemex audit replay artifact differs: {relative}")
        return AuditResult(output_root=audit_root, manifest=replay_manifest, replayed=True)
    sidecar_rows = [
        json.loads(line)
        for line in (one_root / "new_core/sidecar.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    selected = _stratified_audit_indices(sidecar_rows, loaded.config.audit.fraction)
    audit_client = auditor or lemex_audit_provider(loaded)
    audit_rows: list[dict[str, object]] = []
    unknown_review_rows: list[dict[str, object]] = []
    audit_calls: list[ProviderCallResult] = []
    disagreements = 0
    for index in selected:
        row = sidecar_rows[index]
        reference_repr = row.get("reference_repr")
        candidate_repr = row.get("candidate_repr")
        if not isinstance(reference_repr, dict) or not isinstance(candidate_repr, dict):
            raise OneRootPipelineError("audit sidecar lacks REPR objects")
        reference_record = reference_repr.get("record")
        candidate_record = candidate_repr.get("record")
        if not isinstance(reference_record, dict) or not isinstance(candidate_record, dict):
            raise OneRootPipelineError("audit sidecar lacks REPR records")
        statement_a = reference_record.get("goal_v1")
        statement_b = candidate_record.get("goal_v1")
        if not isinstance(statement_a, str) or not isinstance(statement_b, str):
            raise OneRootPipelineError("audit REPR goal is not text")
        prompt = render_blinded_judge_prompt(
            loaded,
            statement_a=statement_a,
            statement_b=statement_b,
        )
        call = audit_client.call(
            prompt=prompt,
            input_ids=(str(row["row_id"]), "blinded_lemex_audit"),
        )
        audit_calls.append(call)
        judgment = JudgeOutput.model_validate(call.structured)
        claude = row["claude_judge"]
        assert isinstance(claude, dict)
        agrees = judgment.verdict == claude.get("verdict")
        disagreements += not agrees
        audit_row = {
            "row_id": row["row_id"],
            "requested_polarity": row["requested_polarity"],
            "claude_verdict": claude.get("verdict"),
            "lemex_judgment": judgment.model_dump(mode="json"),
            "agrees": agrees,
            "action": "retain" if agrees else "unknown_review_exclude_core",
            "call_key": call.call_key,
            "prompt_hash": prompt_hash(prompt),
            "cost_usd": call.cost_usd,
            "usage": call.usage,
        }
        audit_rows.append(audit_row)
        if not agrees:
            unknown_review_rows.append({**audit_row, "training_eligible": False})
    if isinstance(loaded.config, SFT2AOpusConfig):
        ceiling = loaded.config.pilot.ceilings
        if len(audit_calls) > ceiling.maximum_lemex_calls:
            raise OneRootPipelineError("Lemex audit call ceiling exceeded")
        if len(audit_calls) > ceiling.maximum_provider_calls:
            raise OneRootPipelineError("Lemex audit total provider-call ceiling exceeded")
    rows_payload = _canonical_jsonl(audit_rows)
    _atomic(audit_root / "audit/rows.jsonl", rows_payload)
    unknown_payload = _canonical_jsonl(unknown_review_rows)
    _atomic(audit_root / "unknown_review/rows.jsonl", unknown_payload)
    final_manifest: dict[str, object] = {
        "version": "leanfaith_sft2a_lemex_audit_v1",
        "config_hash": loaded.config_hash,
        "source_run_manifest_sha256": hash_file(one_root / "manifest.json"),
        "one_root_manifest_sha256": hash_file(one_root / "manifest.json"),
        "replay_receipt_sha256": hash_file(replay_receipt),
        "target_fraction": loaded.config.audit.fraction,
        "population_rows": len(sidecar_rows),
        "selected_rows": len(selected),
        "realized_fraction": 0.0 if not sidecar_rows else len(selected) / len(sidecar_rows),
        "small_n_stratum_rounding": True,
        "disagreements": disagreements,
        "providers": {
            "opus_source_judge": loaded.config.claude_judge.model_dump(mode="json"),
            "lemex_auditor": loaded.config.lemex_auditor.model_dump(mode="json"),
            "server_revision_limitation": "both configured model aliases are floating",
        },
        "prompt": {
            "artifact": loaded.config.prompts.blinded_claude_judge.model_dump(mode="json"),
            "call_prompt_hashes": [row["prompt_hash"] for row in audit_rows],
        },
        "llm": {
            "calls": len(audit_calls),
            "cache_hits": sum(call.cache_hit for call in audit_calls),
            "latency_seconds": sum(call.elapsed_seconds for call in audit_calls),
            "nominal_cost_usd": sum(call.cost_usd or 0.0 for call in audit_calls),
            "executed_cost_usd": sum(
                call.cost_usd or 0.0 for call in audit_calls if not call.cache_hit
            ),
            "usage": [
                {
                    "provider_id": call.provider_id,
                    "call_key": call.call_key,
                    "cache_hit": call.cache_hit,
                    "usage": call.usage,
                    "cost_usd": call.cost_usd,
                    "elapsed_seconds": call.elapsed_seconds,
                }
                for call in audit_calls
            ],
            "cost_limitations": {
                "lemex": "unavailable",
                "opus_source_judge": "recorded_in_source_run",
            },
        },
        "systematic_disagreement_blocks_scale": disagreements > 0,
        "artifacts": {
            "audit/rows.jsonl": {
                "sha256": hash_file(audit_root / "audit/rows.jsonl"),
                "rows": len(audit_rows),
            },
            "unknown_review/rows.jsonl": {
                "sha256": hash_file(audit_root / "unknown_review/rows.jsonl"),
                "rows": len(unknown_review_rows),
            },
        },
        "scale_50k_started": False,
        "published": False,
    }
    _atomic(manifest_path, canonical_json_bytes(final_manifest) + b"\n")
    return AuditResult(output_root=audit_root, manifest=final_manifest, replayed=False)


__all__ = [
    "AuditResult",
    "OneRootPipelineError",
    "OneRootResult",
    "run_lemex_audit",
    "run_one_root",
    "verify_one_root_replay",
]
