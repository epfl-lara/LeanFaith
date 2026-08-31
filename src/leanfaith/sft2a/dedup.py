"""Persistent cross-root candidate deduplication for SFT2A pilots."""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical
from leanfaith.representations.views import signature_near_dup_hash


class CandidateDedupError(RuntimeError):
    """A candidate claim journal is invalid or internally conflicting."""


class PersistentCandidateRegistry:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _keys(
        raw_signature: str, rendered_goal: str, closed_expr_hash: str | None
    ) -> tuple[str, ...]:
        normalized = " ".join(raw_signature.split())
        keys = (
            "raw:" + hash_canonical(normalized),
            "rendered:" + signature_near_dup_hash(rendered_goal),
        )
        return keys if closed_expr_hash is None else (*keys, "closed_expr:" + closed_expr_hash)

    @staticmethod
    def _claims(raw: bytes) -> dict[str, str]:
        claims: dict[str, str] = {}
        for number, line in enumerate(raw.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise CandidateDedupError(
                    f"invalid candidate registry at line {number}: {exc}"
                ) from exc
            if not isinstance(event, dict):
                raise CandidateDedupError("candidate registry row is not an object")
            owner = event.get("owner")
            keys = event.get("keys")
            if (
                not isinstance(owner, str)
                or not isinstance(keys, list)
                or any(not isinstance(key, str) for key in keys)
            ):
                raise CandidateDedupError("candidate registry claim is malformed")
            for key in keys:
                assert isinstance(key, str)
                previous = claims.get(key)
                if previous is not None and previous != owner:
                    raise CandidateDedupError("candidate registry contains conflicting owners")
                claims[key] = owner
        return claims

    def claim(
        self,
        *,
        raw_signature: str,
        rendered_goal: str,
        owner: str,
        closed_expr_hash: str | None = None,
    ) -> bool:
        keys = self._keys(raw_signature, rendered_goal, closed_expr_hash)
        event = {
            "version": "leanfaith_sft2a_candidate_claim_v1",
            "claim_id": "sft2a-candidate-claim:" + hash_canonical({"keys": keys, "owner": owner}),
            "owner": owner,
            "keys": list(keys),
        }
        line = canonical_json_bytes(event) + b"\n"
        with self.path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0)
                existing = handle.read()
                claims = self._claims(existing)
                owners = {claims[key] for key in keys if key in claims}
                if owners and owners != {owner}:
                    return False
                if owners == {owner}:
                    return True
                handle.seek(0, os.SEEK_END)
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
                return True
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def snapshot(self) -> dict[str, object]:
        raw = self.path.read_bytes() if self.path.exists() else b""
        claims = self._claims(raw)
        owners = sorted(set(claims.values()))
        return {
            "claimed_keys": len(claims),
            "claimed_candidates": len(owners),
            "registry_hash": hash_canonical(claims),
        }


__all__ = ["CandidateDedupError", "PersistentCandidateRegistry"]
