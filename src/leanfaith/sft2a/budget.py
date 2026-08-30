"""Persistent, restart-safe provider-call and reported-Opus-spend ceilings."""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Sequence
from pathlib import Path

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical
from leanfaith.sft2a.models import ExecutionCeilings
from leanfaith.sft2a.pipeline import StructuredProvider
from leanfaith.sft2a.providers import ProviderCallResult


class ProviderBudgetError(RuntimeError):
    """A persistent provider call, reported cost, or ceiling contract failed."""


def _integer(mapping: dict[str, object], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ProviderBudgetError(f"provider budget field {key!r} is not an integer")
    return value


def _number(mapping: dict[str, object], key: str) -> float:
    value = mapping.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ProviderBudgetError(f"provider budget field {key!r} is not numeric")
    return float(value)


class PersistentProviderBudget:
    """An append-only unique-call ledger shared by every resumed pilot process."""

    def __init__(self, path: Path, ceilings: ExecutionCeilings) -> None:
        self.path = path
        self.ceilings = ceilings
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _events(self) -> dict[str, dict[str, object]]:
        if not self.path.exists():
            return {}
        events: dict[str, dict[str, object]] = {}
        with self.path.open("rb") as handle:
            for number, raw in enumerate(handle, start=1):
                if not raw.strip():
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise ProviderBudgetError(
                        f"invalid provider budget journal at line {number}: {exc}"
                    ) from exc
                if not isinstance(event, dict):
                    raise ProviderBudgetError("provider budget journal row is not an object")
                call_key = event.get("call_key")
                if not isinstance(call_key, str) or not call_key:
                    raise ProviderBudgetError("provider budget journal lacks a call key")
                previous = events.get(call_key)
                if previous is not None and previous != event:
                    raise ProviderBudgetError("provider budget call-key conflict")
                events[call_key] = event
        return events

    def snapshot(self) -> dict[str, object]:
        events = self._events()
        proposer = sum(1 for event in events.values() if event.get("kind") == "proposer")
        opus = sum(1 for event in events.values() if event.get("kind") == "opus")
        lemex = sum(1 for event in events.values() if event.get("kind") == "lemex")
        opus_spend = sum(
            (
                _number(event, "charged_reported_cost_usd")
                for event in events.values()
                if event.get("kind") == "opus"
            ),
            start=0.0,
        )
        return {
            "unique_provider_calls": len(events),
            "proposer_calls": proposer,
            "opus_calls": opus,
            "lemex_calls": lemex,
            "reported_opus_spend_usd": opus_spend,
            "ceilings": self.ceilings.model_dump(mode="json"),
            "ledger_hash": hash_canonical(list(events.values())),
        }

    def ensure_can_attempt(self, kind: str) -> None:
        snapshot = self.snapshot()
        if _integer(snapshot, "unique_provider_calls") >= self.ceilings.maximum_provider_calls:
            raise ProviderBudgetError("persistent total provider-call ceiling reached")
        key = f"{kind}_calls"
        observed = _integer(snapshot, key)
        maximum = {
            "proposer": self.ceilings.maximum_proposer_calls,
            "opus": self.ceilings.maximum_opus_calls,
            "lemex": self.ceilings.maximum_lemex_calls,
        }.get(kind)
        if maximum is None:
            raise ProviderBudgetError(f"unsupported provider-budget kind: {kind}")
        if observed >= maximum:
            raise ProviderBudgetError(f"persistent {kind} provider-call ceiling reached")
        if (
            kind == "opus"
            and _number(snapshot, "reported_opus_spend_usd")
            >= self.ceilings.maximum_reported_opus_spend_usd
        ):
            raise ProviderBudgetError("persistent reported Opus spend ceiling reached")

    def record(self, *, kind: str, result: ProviderCallResult) -> dict[str, object]:
        if kind not in {"proposer", "opus", "lemex"}:
            raise ProviderBudgetError(f"unsupported provider-budget kind: {kind}")
        if kind == "opus" and result.cost_usd is None:
            raise ProviderBudgetError("Opus call lacks the required reported cost")
        event = {
            "version": "leanfaith_sft2a_provider_budget_event_v1",
            "call_key": result.call_key,
            "kind": kind,
            "provider_id": result.provider_id,
            "provider_reported_cost_usd": result.cost_usd,
            "charged_reported_cost_usd": (
                0.0 if kind == "opus" and result.cache_hit else result.cost_usd
            ),
            "provider_cache_hit_at_record": result.cache_hit,
        }
        line = canonical_json_bytes(event) + b"\n"
        with self.path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0)
                for raw in handle.read().splitlines():
                    try:
                        previous = json.loads(raw)
                    except json.JSONDecodeError as exc:
                        raise ProviderBudgetError(
                            "provider budget journal became invalid while locked"
                        ) from exc
                    if isinstance(previous, dict) and previous.get("call_key") == result.call_key:
                        same_call = (
                            previous.get("kind") == kind
                            and previous.get("provider_id") == result.provider_id
                            and previous.get("provider_reported_cost_usd") == result.cost_usd
                        )
                        if not same_call:
                            raise ProviderBudgetError("provider budget call-key conflict")
                        return self.snapshot()
                handle.seek(0, os.SEEK_END)
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        snapshot = self.snapshot()
        if _integer(snapshot, "unique_provider_calls") > self.ceilings.maximum_provider_calls:
            raise ProviderBudgetError("persistent total provider-call ceiling exceeded")
        per_kind = _integer(snapshot, f"{kind}_calls")
        maximum = {
            "proposer": self.ceilings.maximum_proposer_calls,
            "opus": self.ceilings.maximum_opus_calls,
            "lemex": self.ceilings.maximum_lemex_calls,
        }[kind]
        if per_kind > maximum:
            raise ProviderBudgetError(f"persistent {kind} provider-call ceiling exceeded")
        if (
            _number(snapshot, "reported_opus_spend_usd")
            > self.ceilings.maximum_reported_opus_spend_usd
        ):
            raise ProviderBudgetError("persistent reported Opus spend ceiling exceeded")
        return snapshot


class BudgetedProvider:
    def __init__(
        self,
        inner: StructuredProvider,
        *,
        kind: str,
        budget: PersistentProviderBudget,
    ) -> None:
        self.inner = inner
        self.kind = kind
        self.budget = budget

    def call(self, *, prompt: str, input_ids: Sequence[str]) -> ProviderCallResult:
        self.budget.ensure_can_attempt(self.kind)
        result = self.inner.call(prompt=prompt, input_ids=input_ids)
        self.budget.record(kind=self.kind, result=result)
        return result


__all__ = ["BudgetedProvider", "PersistentProviderBudget", "ProviderBudgetError"]
