"""Offline tests for the fail-closed Sol/Fable authorization bundle."""

from __future__ import annotations

import datetime
import json
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from leanfaith.config.hashing import canonical_json_bytes, hash_file, sha256_hex
from leanfaith.generation import lf022_dual_judge_authorization_v1 as dual
from leanfaith.generation.claude_fable_judge_v1 import (
    ClaudeFableLiveAuthorization,
    load_claude_fable_judge_config,
)
from leanfaith.generation.codex_sol_judge_v1 import (
    CodexSolLiveAuthorization,
    load_codex_sol_judge_config,
)
from leanfaith.generation.lf022_dual_judge_authorization_v1 import (
    LF022DualJudgeAuthorizationError,
    LF022DualJudgeAuthorizationManifest,
    create_lf022_dual_judge_authorization,
)
from leanfaith.generation.lf022_weak_batch import (
    BoundArtifact,
    JudgeEndpointPin,
    LF022WeakBatchSpec,
)
from leanfaith.generation.providers import DecodingValue

REPO_ROOT = Path(".").resolve()
SOL_CONFIG = REPO_ROOT / "configs/generation/lf022_codex_sol_judge_v1.yaml"
FABLE_CONFIG = REPO_ROOT / "configs/generation/lf022_claude_fable_judge_v1.yaml"
NOW = datetime.datetime(2026, 8, 12, 10, 30, tzinfo=datetime.UTC)
BATCH_ID = "lf022_weak_batch:" + "a" * 64
PAIR_IDS = tuple("pair:" + character * 64 for character in "bcdef")
NONCES = tuple(bytes([index]) * 48 for index in range(1, 5))


def _fable_decoding() -> dict[str, DecodingValue]:
    config = load_claude_fable_judge_config(FABLE_CONFIG).config
    return {
        "effort": config.effort,
        "system_prompt_sha256": config.system_prompt_sha256,
        "output_schema_sha256": config.output_schema_sha256,
        "claude_cli_version": config.claude_cli_version,
        "claude_binary_sha256": config.claude_binary_sha256,
        "structured_output": True,
        "safe_mode": True,
        "tools_disabled": True,
        "session_persistence": False,
    }


def _spec(*, live_authorized: bool = True) -> LF022WeakBatchSpec:
    sol = load_codex_sol_judge_config(SOL_CONFIG).config
    fable = load_claude_fable_judge_config(FABLE_CONFIG).config
    return LF022WeakBatchSpec(
        batch_name="authorization-fixture",
        candidate_manifest=BoundArtifact(path="unused", sha256="1" * 64),
        candidate_records=BoundArtifact(path="unused", sha256="2" * 64),
        weak_supervision_config=BoundArtifact(path="unused", sha256="3" * 64),
        production_family_matrix=BoundArtifact(path="unused", sha256="4" * 64),
        randomization_key_sha256="5" * 64,
        judge_a=JudgeEndpointPin(
            provider_slot="judge_A",
            provider=sol.provider,
            model=sol.registry_model_id,
            family_id=sol.model_family,
            revision=sol.endpoint_revision,
            decoding=sol.endpoint_decoding,
        ),
        judge_b=JudgeEndpointPin(
            provider_slot="judge_B",
            provider=fable.provider,
            model=fable.registry_model_id,
            family_id=fable.model_family,
            revision=fable.endpoint_revision,
            decoding=_fable_decoding(),
        ),
        primary_eval_family_id="held_out_eval_family",
        execution_authorization=(
            "live_provider_calls_explicitly_authorized"
            if live_authorized
            else "offline_fixture_or_replay_only"
        ),
        live_provider_calls_authorized=live_authorized,
    )


def _fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    spec: LF022WeakBatchSpec | None = None,
    pair_ids: tuple[str, ...] = PAIR_IDS,
) -> Path:
    batch_root = tmp_path / "batch"
    batch_root.mkdir(exist_ok=True)
    (batch_root / "dispatch_manifest.json").write_bytes(b"exact-dispatch-manifest\n")
    dispatch = SimpleNamespace(batch_id=BATCH_ID)
    records = tuple(SimpleNamespace(pair_id=pair_id) for pair_id in pair_ids)
    monkeypatch.setattr(
        dual,
        "_load_prepared_batch",
        lambda _root: (spec or _spec(), dispatch, records, {}),
    )
    return batch_root


def _create(
    *,
    batch_root: Path,
    output_dir: Path,
    now: datetime.datetime = NOW,
    offset_pairs: int = 1,
    limit_pairs: int = 2,
    shard_id: str = "fresh-qwen-001",
    validity_minutes: int = 90,
    approved_by: str = "offline-test-reviewer",
) -> LF022DualJudgeAuthorizationManifest:
    return create_lf022_dual_judge_authorization(
        batch_root=batch_root,
        output_dir=output_dir,
        sol_config_path=SOL_CONFIG,
        fable_config_path=FABLE_CONFIG,
        shard_id=shard_id,
        offset_pairs=offset_pairs,
        limit_pairs=limit_pairs,
        approved_by=approved_by,
        validity_minutes=validity_minutes,
        now=now,
    )


def _load_json(path: Path) -> dict[str, object]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_bundle_is_private_canonical_and_bound_to_exact_batch_configs_and_shard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch_root = _fixture(tmp_path, monkeypatch)
    generated = iter(NONCES)
    monkeypatch.setattr(
        "leanfaith.generation.lf022_dual_judge_authorization_v1.secrets.token_bytes",
        lambda size: next(generated),
    )
    output = tmp_path / "authorization"

    manifest = _create(batch_root=batch_root, output_dir=output)

    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert {item.name for item in output.iterdir()} == {
        "manifest.json",
        "sol.authorization.json",
        "sol.authorization_nonce",
        "sol.run_nonce",
        "fable.authorization.json",
        "fable.authorization_nonce",
        "fable.run_nonce",
    }
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in output.iterdir())
    assert (output / "sol.authorization_nonce").read_bytes() == NONCES[0]
    assert (output / "sol.run_nonce").read_bytes() == NONCES[1]
    assert (output / "fable.authorization_nonce").read_bytes() == NONCES[2]
    assert (output / "fable.run_nonce").read_bytes() == NONCES[3]

    sol_loaded = load_codex_sol_judge_config(SOL_CONFIG)
    fable_loaded = load_claude_fable_judge_config(FABLE_CONFIG)
    sol = CodexSolLiveAuthorization.model_validate(_load_json(output / "sol.authorization.json"))
    fable = ClaudeFableLiveAuthorization.model_validate(
        _load_json(output / "fable.authorization.json")
    )
    for authorization, slot, config_sha, nonce in (
        (sol, "judge_A", sol_loaded.sha256, NONCES[0]),
        (fable, "judge_B", fable_loaded.sha256, NONCES[2]),
    ):
        assert authorization.batch_id == BATCH_ID
        assert authorization.judge_slot == slot
        assert authorization.config_sha256 == config_sha
        assert authorization.shard_id == "fresh-qwen-001"
        assert authorization.offset_pairs == 1
        assert authorization.limit_pairs == 2
        assert authorization.approved_at == NOW
        assert authorization.expires_at == NOW + datetime.timedelta(minutes=90)
        assert authorization.authorization_nonce_sha256 == sha256_hex(nonce)
        assert authorization.public_external_execution_authorized
        assert not authorization.private_source_content_authorized
        assert not authorization.semantic_labels_created
        assert not authorization.gate_credit_claimed

    assert manifest.batch_id == BATCH_ID
    assert manifest.dispatch_manifest_sha256 == hash_file(batch_root / "dispatch_manifest.json")
    assert manifest.sol_authorization_id == sol.authorization_id
    assert manifest.fable_authorization_id == fable.authorization_id
    assert manifest.sol_authorization_sha256 == hash_file(output / "sol.authorization.json")
    assert manifest.fable_authorization_sha256 == hash_file(output / "fable.authorization.json")
    assert manifest.sol_authorization_nonce_sha256 == sha256_hex(NONCES[0])
    assert manifest.sol_run_nonce_sha256 == sha256_hex(NONCES[1])
    assert manifest.fable_authorization_nonce_sha256 == sha256_hex(NONCES[2])
    assert manifest.fable_run_nonce_sha256 == sha256_hex(NONCES[3])
    assert not manifest.private_source_content_authorized
    assert not manifest.semantic_labels_created
    assert not manifest.silver_records_created
    assert not manifest.training_eligible
    assert not manifest.evaluation_eligible
    assert not manifest.gate_credit_claimed
    assert (output / "manifest.json").read_bytes() == (
        canonical_json_bytes(manifest.model_dump(mode="json")) + b"\n"
    )


def test_fixed_time_and_nonce_stream_reproduce_bundle_identity_and_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch_root = _fixture(tmp_path, monkeypatch)

    def create_at(output: Path) -> LF022DualJudgeAuthorizationManifest:
        generated = iter(NONCES)
        monkeypatch.setattr(
            "leanfaith.generation.lf022_dual_judge_authorization_v1.secrets.token_bytes",
            lambda size: next(generated),
        )
        return _create(batch_root=batch_root, output_dir=output)

    first = create_at(tmp_path / "first")
    second = create_at(tmp_path / "second")

    assert first == second
    for name in (
        "manifest.json",
        "sol.authorization.json",
        "sol.authorization_nonce",
        "sol.run_nonce",
        "fable.authorization.json",
        "fable.authorization_nonce",
        "fable.run_nonce",
    ):
        assert (tmp_path / "first" / name).read_bytes() == (tmp_path / "second" / name).read_bytes()


def test_secret_nonce_bytes_never_appear_in_json_or_console(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    batch_root = _fixture(tmp_path, monkeypatch)
    distinct_ascii_nonces = tuple(
        (f"private-nonce-{index}-" + "x" * 31).encode() for index in range(4)
    )
    assert all(len(value) == 47 for value in distinct_ascii_nonces)
    # The test double honors the requested size without exposing a real secret.
    generated = iter(value + b"!" for value in distinct_ascii_nonces)
    monkeypatch.setattr(
        "leanfaith.generation.lf022_dual_judge_authorization_v1.secrets.token_bytes",
        lambda size: next(generated),
    )
    output = tmp_path / "authorization"

    _create(batch_root=batch_root, output_dir=output)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    json_bytes = (output / "manifest.json").read_bytes()
    json_bytes += (output / "sol.authorization.json").read_bytes()
    json_bytes += (output / "fable.authorization.json").read_bytes()
    for nonce in tuple(value + b"!" for value in distinct_ascii_nonces):
        assert nonce not in json_bytes


@pytest.mark.parametrize("validity_minutes", [0, -1, 1441])
def test_invalid_validity_is_rejected_without_creating_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    validity_minutes: int,
) -> None:
    batch_root = _fixture(tmp_path, monkeypatch)
    output = tmp_path / "authorization"

    with pytest.raises(LF022DualJudgeAuthorizationError, match="validity_minutes"):
        _create(
            batch_root=batch_root,
            output_dir=output,
            validity_minutes=validity_minutes,
        )

    assert not output.exists()


def test_naive_approval_time_is_rejected_without_creating_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch_root = _fixture(tmp_path, monkeypatch)
    output = tmp_path / "authorization"

    with pytest.raises(LF022DualJudgeAuthorizationError, match="timezone-aware"):
        _create(
            batch_root=batch_root,
            output_dir=output,
            now=datetime.datetime(2026, 8, 12, 10, 30),
        )

    assert not output.exists()


@pytest.mark.parametrize(
    ("offset_pairs", "limit_pairs"),
    [(-1, 1), (0, 0), (4, 2), (5, 1)],
)
def test_invalid_shard_bounds_are_rejected_without_creating_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    offset_pairs: int,
    limit_pairs: int,
) -> None:
    batch_root = _fixture(tmp_path, monkeypatch)
    output = tmp_path / "authorization"

    with pytest.raises(LF022DualJudgeAuthorizationError, match="exceeds prepared pairs"):
        _create(
            batch_root=batch_root,
            output_dir=output,
            offset_pairs=offset_pairs,
            limit_pairs=limit_pairs,
        )

    assert not output.exists()


@pytest.mark.parametrize(
    ("shard_id", "approved_by", "match"),
    [("unsafe shard", "reviewer", "shard_id"), ("safe", "", "approved_by")],
)
def test_invalid_authorization_fields_are_rejected_before_output_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shard_id: str,
    approved_by: str,
    match: str,
) -> None:
    batch_root = _fixture(tmp_path, monkeypatch)
    output = tmp_path / "authorization"

    with pytest.raises(ValidationError, match=match):
        _create(
            batch_root=batch_root,
            output_dir=output,
            shard_id=shard_id,
            approved_by=approved_by,
        )

    assert not output.exists()


def test_live_authorization_and_safe_endpoint_pins_are_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "authorization"
    batch_root = _fixture(tmp_path, monkeypatch, spec=_spec(live_authorized=False))
    with pytest.raises(LF022DualJudgeAuthorizationError, match="not authorized"):
        _create(batch_root=batch_root, output_dir=output)
    assert not output.exists()

    unsafe = _spec().model_copy(deep=True)
    unsafe.judge_b.decoding["safe_mode"] = False
    batch_root = _fixture(tmp_path, monkeypatch, spec=unsafe)
    with pytest.raises(LF022DualJudgeAuthorizationError, match="Fable endpoint differs"):
        _create(batch_root=batch_root, output_dir=output)
    assert not output.exists()


@pytest.mark.parametrize("slot", ["sol", "fable"])
def test_config_or_endpoint_drift_is_rejected_before_output_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    slot: str,
) -> None:
    spec = _spec().model_copy(deep=True)
    endpoint = spec.judge_a if slot == "sol" else spec.judge_b
    drifted_endpoint = endpoint.model_copy(
        update={"revision": "provider-deployment-snapshot:" + "0" * 64}
    )
    spec = spec.model_copy(update={"judge_a" if slot == "sol" else "judge_b": drifted_endpoint})
    batch_root = _fixture(tmp_path, monkeypatch, spec=spec)
    output = tmp_path / "authorization"

    with pytest.raises(
        LF022DualJudgeAuthorizationError,
        match=r"prepared Sol endpoint differs|prepared Fable endpoint differs",
    ):
        _create(batch_root=batch_root, output_dir=output)

    assert not output.exists()


def test_existing_symlink_relative_and_missing_parent_outputs_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch_root = _fixture(tmp_path, monkeypatch)
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(LF022DualJudgeAuthorizationError, match="must not already exist"):
        _create(batch_root=batch_root, output_dir=existing)

    target = tmp_path / "target"
    target.mkdir()
    symlink = tmp_path / "symlink"
    symlink.symlink_to(target, target_is_directory=True)
    with pytest.raises(LF022DualJudgeAuthorizationError, match="must not already exist"):
        _create(batch_root=batch_root, output_dir=symlink)

    with pytest.raises(LF022DualJudgeAuthorizationError, match="must be absolute"):
        _create(batch_root=batch_root, output_dir=Path("relative-output"))

    missing_parent = tmp_path / "missing" / "authorization"
    with pytest.raises(LF022DualJudgeAuthorizationError, match="parent is missing"):
        _create(batch_root=batch_root, output_dir=missing_parent)


def test_existing_private_file_is_never_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch_root = _fixture(tmp_path, monkeypatch)
    output = tmp_path / "authorization"
    output.mkdir(mode=0o700)
    protected = output / "sol.authorization.json"
    protected.write_bytes(b"do-not-overwrite")

    with pytest.raises(LF022DualJudgeAuthorizationError, match="must not already exist"):
        _create(batch_root=batch_root, output_dir=output)

    assert protected.read_bytes() == b"do-not-overwrite"
