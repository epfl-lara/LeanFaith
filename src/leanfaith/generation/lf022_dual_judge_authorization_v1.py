"""Create one fail-closed Sol/Fable live-authorization bundle.

The command creates no provider calls.  It verifies an already prepared public
LF-022 weak batch, binds the exact current Sol/xhigh and Fable/max adapters, and
writes short-lived authorization and nonce files into a new private directory.
Secret nonce bytes are never printed.
"""

from __future__ import annotations

import argparse
import datetime
import os
import secrets
import stat
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_file, sha256_hex
from leanfaith.config.models import StrictModel
from leanfaith.generation.claude_fable_judge_v1 import (
    ClaudeFableLiveAuthorization,
    load_claude_fable_judge_config,
)
from leanfaith.generation.codex_sol_judge_v1 import (
    CodexSolLiveAuthorization,
    load_codex_sol_judge_config,
)
from leanfaith.generation.lf022_weak_batch import _endpoint, _load_prepared_batch
from leanfaith.schemas.ids import HEX64_PATTERN, id_pattern, make_id

AUTHORIZATION_BUNDLE_METHOD: Literal["lf022_dual_judge_authorization_v1"] = (
    "lf022_dual_judge_authorization_v1"
)
_NONCE_BYTES = 48


class LF022DualJudgeAuthorizationError(RuntimeError):
    """A batch, endpoint, output-safety, or authorization invariant failed."""


class LF022DualJudgeAuthorizationManifest(StrictModel):
    """Content-addressed receipt for one private authorization bundle."""

    schema_version: Literal[1] = 1
    method_version: Literal["lf022_dual_judge_authorization_v1"] = AUTHORIZATION_BUNDLE_METHOD
    bundle_id: str = Field(pattern=id_pattern("lf022_dual_judge_authorization"))
    batch_id: str = Field(pattern=id_pattern("lf022_weak_batch"))
    dispatch_manifest_sha256: str = Field(pattern=HEX64_PATTERN)
    shard_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.-]+$")
    offset_pairs: int = Field(ge=0, strict=True)
    limit_pairs: int = Field(ge=1, le=64, strict=True)
    approved_at: datetime.datetime
    expires_at: datetime.datetime
    approved_by: str = Field(min_length=1, max_length=200)
    sol_config_sha256: str = Field(pattern=HEX64_PATTERN)
    fable_config_sha256: str = Field(pattern=HEX64_PATTERN)
    sol_authorization_id: str = Field(pattern=id_pattern("lf022_sol_live_authorization"))
    sol_authorization_sha256: str = Field(pattern=HEX64_PATTERN)
    sol_authorization_nonce_sha256: str = Field(pattern=HEX64_PATTERN)
    sol_run_nonce_sha256: str = Field(pattern=HEX64_PATTERN)
    fable_authorization_id: str = Field(pattern=id_pattern("lf022_fable_live_authorization"))
    fable_authorization_sha256: str = Field(pattern=HEX64_PATTERN)
    fable_authorization_nonce_sha256: str = Field(pattern=HEX64_PATTERN)
    fable_run_nonce_sha256: str = Field(pattern=HEX64_PATTERN)
    public_external_execution_authorized: Literal[True] = True
    private_source_content_authorized: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    silver_records_created: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _identity(self) -> Self:
        if self.approved_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("authorization timestamps must be timezone-aware")
        if self.expires_at <= self.approved_at:
            raise ValueError("authorization expiry must follow approval")
        expected = make_id(
            "lf022_dual_judge_authorization",
            self.model_dump(mode="json", exclude={"bundle_id"}),
        )
        if self.bundle_id != expected:
            raise ValueError("authorization bundle ID differs from content")
        return self


def _new_private_directory(path: Path) -> Path:
    if not path.is_absolute():
        raise LF022DualJudgeAuthorizationError("authorization output must be absolute")
    if path.exists() or path.is_symlink():
        raise LF022DualJudgeAuthorizationError("authorization output must not already exist")
    try:
        parent = path.parent.resolve(strict=True)
    except OSError as exc:
        raise LF022DualJudgeAuthorizationError("authorization output parent is missing") from exc
    if path.parent.absolute() != parent or not parent.is_dir() or parent.is_symlink():
        raise LF022DualJudgeAuthorizationError("authorization output parent is unsafe")
    os.mkdir(path, mode=0o700)
    if stat.S_IMODE(path.stat().st_mode) != 0o700:
        raise LF022DualJudgeAuthorizationError("authorization output is not private")
    return path


def _write_private(path: Path, payload: bytes) -> str:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise LF022DualJudgeAuthorizationError(f"cannot create private file {path.name}") from exc
    with os.fdopen(descriptor, "wb") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    if stat.S_IMODE(path.stat().st_mode) != 0o600 or path.read_bytes() != payload:
        raise LF022DualJudgeAuthorizationError(f"private file verification failed: {path.name}")
    return sha256_hex(payload)


def create_lf022_dual_judge_authorization(
    *,
    batch_root: Path,
    output_dir: Path,
    sol_config_path: Path,
    fable_config_path: Path,
    shard_id: str,
    offset_pairs: int,
    limit_pairs: int,
    approved_by: str,
    validity_minutes: int,
    now: datetime.datetime | None = None,
) -> LF022DualJudgeAuthorizationManifest:
    """Verify one public shard and write Sol/Fable authorization material."""

    if validity_minutes < 1 or validity_minutes > 24 * 60:
        raise LF022DualJudgeAuthorizationError("validity_minutes must be within 1..1440")
    spec, dispatch, records, _ = _load_prepared_batch(batch_root)
    if not spec.live_provider_calls_authorized or (
        spec.execution_authorization != "live_provider_calls_explicitly_authorized"
    ):
        raise LF022DualJudgeAuthorizationError("prepared batch is not authorized for live calls")
    pair_ids = sorted({item.pair_id for item in records})
    if offset_pairs < 0 or limit_pairs < 1 or offset_pairs + limit_pairs > len(pair_ids):
        raise LF022DualJudgeAuthorizationError("authorization shard exceeds prepared pairs")

    sol_loaded = load_codex_sol_judge_config(sol_config_path)
    fable_loaded = load_claude_fable_judge_config(fable_config_path)
    sol_endpoint = _endpoint(spec, "judge_A")
    fable_endpoint = _endpoint(spec, "judge_B")
    if (
        sol_endpoint.provider != sol_loaded.config.provider
        or sol_endpoint.model != sol_loaded.config.registry_model_id
        or sol_endpoint.family_id != sol_loaded.config.model_family
        or sol_endpoint.revision != sol_loaded.config.endpoint_revision
        or sol_endpoint.decoding != sol_loaded.config.endpoint_decoding
    ):
        raise LF022DualJudgeAuthorizationError("prepared Sol endpoint differs from config")
    fable_decoding = {
        "effort": fable_loaded.config.effort,
        "system_prompt_sha256": fable_loaded.config.system_prompt_sha256,
        "output_schema_sha256": fable_loaded.config.output_schema_sha256,
        "claude_cli_version": fable_loaded.config.claude_cli_version,
        "claude_binary_sha256": fable_loaded.config.claude_binary_sha256,
        "structured_output": True,
        "safe_mode": True,
        "tools_disabled": True,
        "session_persistence": False,
    }
    if (
        fable_endpoint.provider != fable_loaded.config.provider
        or fable_endpoint.model != fable_loaded.config.registry_model_id
        or fable_endpoint.family_id != fable_loaded.config.model_family
        or fable_endpoint.revision != fable_loaded.config.endpoint_revision
        or fable_endpoint.decoding != fable_decoding
    ):
        raise LF022DualJudgeAuthorizationError("prepared Fable endpoint differs from config")

    approved_at = now or datetime.datetime.now(datetime.UTC)
    if approved_at.tzinfo is None:
        raise LF022DualJudgeAuthorizationError("approval time must be timezone-aware")
    approved_at = approved_at.astimezone(datetime.UTC)
    expires_at = approved_at + datetime.timedelta(minutes=validity_minutes)
    approved_at_json = approved_at.isoformat().replace("+00:00", "Z")
    expires_at_json = expires_at.isoformat().replace("+00:00", "Z")
    sol_auth_nonce = secrets.token_bytes(_NONCE_BYTES)
    sol_run_nonce = secrets.token_bytes(_NONCE_BYTES)
    fable_auth_nonce = secrets.token_bytes(_NONCE_BYTES)
    fable_run_nonce = secrets.token_bytes(_NONCE_BYTES)

    common = {
        "schema_version": 1,
        "batch_id": dispatch.batch_id,
        "shard_id": shard_id,
        "offset_pairs": offset_pairs,
        "limit_pairs": limit_pairs,
        "approved_at": approved_at_json,
        "expires_at": expires_at_json,
        "approved_by": approved_by,
        "public_external_execution_authorized": True,
        "private_source_content_authorized": False,
        "semantic_labels_created": False,
        "gate_credit_claimed": False,
    }
    sol_values = {
        **common,
        "config_sha256": sol_loaded.sha256,
        "judge_slot": "judge_A",
        "authorization_nonce_sha256": sha256_hex(sol_auth_nonce),
    }
    sol_auth = CodexSolLiveAuthorization.model_validate(
        {
            **sol_values,
            "authorization_id": make_id("lf022_sol_live_authorization", sol_values),
        }
    )
    fable_values = {
        **common,
        "config_sha256": fable_loaded.sha256,
        "judge_slot": "judge_B",
        "authorization_nonce_sha256": sha256_hex(fable_auth_nonce),
    }
    fable_auth = ClaudeFableLiveAuthorization.model_validate(
        {
            **fable_values,
            "authorization_id": make_id("lf022_fable_live_authorization", fable_values),
        }
    )

    output = _new_private_directory(output_dir)
    sol_auth_bytes = canonical_json_bytes(sol_auth.model_dump(mode="json")) + b"\n"
    fable_auth_bytes = canonical_json_bytes(fable_auth.model_dump(mode="json")) + b"\n"
    sol_auth_sha = _write_private(output / "sol.authorization.json", sol_auth_bytes)
    _write_private(output / "sol.authorization_nonce", sol_auth_nonce)
    _write_private(output / "sol.run_nonce", sol_run_nonce)
    fable_auth_sha = _write_private(output / "fable.authorization.json", fable_auth_bytes)
    _write_private(output / "fable.authorization_nonce", fable_auth_nonce)
    _write_private(output / "fable.run_nonce", fable_run_nonce)

    values: dict[str, object] = {
        "schema_version": 1,
        "method_version": AUTHORIZATION_BUNDLE_METHOD,
        "batch_id": dispatch.batch_id,
        "dispatch_manifest_sha256": hash_file(batch_root / "dispatch_manifest.json"),
        "shard_id": shard_id,
        "offset_pairs": offset_pairs,
        "limit_pairs": limit_pairs,
        "approved_at": approved_at_json,
        "expires_at": expires_at_json,
        "approved_by": approved_by,
        "sol_config_sha256": sol_loaded.sha256,
        "fable_config_sha256": fable_loaded.sha256,
        "sol_authorization_id": sol_auth.authorization_id,
        "sol_authorization_sha256": sol_auth_sha,
        "sol_authorization_nonce_sha256": sha256_hex(sol_auth_nonce),
        "sol_run_nonce_sha256": sha256_hex(sol_run_nonce),
        "fable_authorization_id": fable_auth.authorization_id,
        "fable_authorization_sha256": fable_auth_sha,
        "fable_authorization_nonce_sha256": sha256_hex(fable_auth_nonce),
        "fable_run_nonce_sha256": sha256_hex(fable_run_nonce),
        "public_external_execution_authorized": True,
        "private_source_content_authorized": False,
        "semantic_labels_created": False,
        "silver_records_created": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    manifest = LF022DualJudgeAuthorizationManifest.model_validate(
        {
            **values,
            "bundle_id": make_id("lf022_dual_judge_authorization", values),
        }
    )
    _write_private(
        output / "manifest.json",
        canonical_json_bytes(manifest.model_dump(mode="json")) + b"\n",
    )
    return manifest


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--sol-config",
        type=Path,
        default=Path("configs/generation/lf022_codex_sol_judge_v1.yaml"),
    )
    parser.add_argument(
        "--fable-config",
        type=Path,
        default=Path("configs/generation/lf022_claude_fable_judge_v1.yaml"),
    )
    parser.add_argument("--shard-id", required=True)
    parser.add_argument("--offset-pairs", type=int, required=True)
    parser.add_argument("--limit-pairs", type=int, required=True)
    parser.add_argument("--approved-by", required=True)
    parser.add_argument("--validity-minutes", type=int, default=120)
    arguments = parser.parse_args()
    result = create_lf022_dual_judge_authorization(
        batch_root=arguments.batch_root,
        output_dir=arguments.output_dir,
        sol_config_path=arguments.sol_config,
        fable_config_path=arguments.fable_config,
        shard_id=arguments.shard_id,
        offset_pairs=arguments.offset_pairs,
        limit_pairs=arguments.limit_pairs,
        approved_by=arguments.approved_by,
        validity_minutes=arguments.validity_minutes,
    )
    print(
        f"bundle_id={result.bundle_id} batch_id={result.batch_id} "
        f"shard_id={result.shard_id} expires_at={result.expires_at.isoformat()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())


__all__ = [
    "AUTHORIZATION_BUNDLE_METHOD",
    "LF022DualJudgeAuthorizationError",
    "LF022DualJudgeAuthorizationManifest",
    "create_lf022_dual_judge_authorization",
]
