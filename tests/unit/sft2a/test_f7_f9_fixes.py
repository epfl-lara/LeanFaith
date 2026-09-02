from __future__ import annotations

from pathlib import Path

from leanfaith.config.hashing import hash_canonical, hash_file
from leanfaith.sft2a.lean_oracle import (
    ORACLE_METHOD_VERSION,
    ORACLE_METHOD_VERSION_V2,
    _signature_command,
)
from leanfaith.sft2a.mechanisms import ALL_MECHANISM_FAMILIES, PRESERVING_MECHANISMS


def test_v1_method_version_is_unchanged() -> None:
    assert ORACLE_METHOD_VERSION == "sft2a_proof_free_signature_oracle_binder_hygiene_v1"


def test_v2_method_version_is_distinct() -> None:
    assert ORACLE_METHOD_VERSION_V2 == "sft2a_proof_free_signature_oracle_canonical_universes_v2"
    assert ORACLE_METHOD_VERSION_V2 != ORACLE_METHOD_VERSION


def test_v1_command_uses_lfSft2aSignature_keyword(tmp_path: Path) -> None:
    from leanfaith.representations.goal_v1 import CompileContext

    context = CompileContext(
        project_id="test",
        project_revision="abc",
        lean_version="leanprover/lean4:v4.99.0",
        import_header="import Lean",
        command_preamble="",
        namespace_context=[],
        open_context=[],
        scoped_context=[],
        options={},
    )
    command = _signature_command(
        context=context,
        signature="True",
        endpoint_id="test-endpoint",
        render_scope_id="test-scope",
        cache_version="v1",
    )
    assert "lfSft2aSignature " in command
    assert "lfSft2aSignatureV2" not in command
    assert "universe u_0" not in command


def test_v2_command_uses_lfSft2aSignatureV2_keyword_and_universes() -> None:
    from leanfaith.representations.goal_v1 import CompileContext

    context = CompileContext(
        project_id="test",
        project_revision="abc",
        lean_version="leanprover/lean4:v4.99.0",
        import_header="import Lean",
        command_preamble="",
        namespace_context=[],
        open_context=[],
        scoped_context=[],
        options={},
    )
    command = _signature_command(
        context=context,
        signature="True",
        endpoint_id="test-endpoint",
        render_scope_id="test-scope",
        cache_version="v2",
    )
    assert "lfSft2aSignatureV2 " in command
    assert "universe u_0 u_1 u_2 u_3 u_4 u_5 u_6 u_7" in command
    assert "assignCanonicalUniverses" in command


def test_v2_cache_key_excludes_oracle_source_sha256() -> None:
    from leanfaith.representations.goal_v1 import CompileContext

    context = CompileContext(
        project_id="test",
        project_revision="abc",
        lean_version="leanprover/lean4:v4.99.0",
        import_header="import Lean",
        command_preamble="",
        namespace_context=[],
        open_context=[],
        scoped_context=[],
        options={},
    )
    v1_payload = {
        "method_version": ORACLE_METHOD_VERSION,
        "signature_sha256": "abc",
        "endpoint_role": "candidate",
        "compile_context": context.canonical_payload(),
        "leaninteract_version": "1.0",
        "repl_revision": "1.0",
        "repr": {},
        "oracle_source_sha256": hash_file(Path(__file__)),
    }
    v2_payload = {
        "method_version": ORACLE_METHOD_VERSION_V2,
        "signature_sha256": "abc",
        "endpoint_role": "candidate",
        "compile_context": context.canonical_payload(),
        "leaninteract_version": "1.0",
        "repl_revision": "1.0",
        "repr": {},
    }
    v1_key = hash_canonical(v1_payload)
    v2_key = hash_canonical(v2_payload)
    assert v1_key != v2_key
    assert "oracle_source_sha256" not in v2_payload


def test_definitional_unfold_refold_is_removed() -> None:
    assert "definitional_unfold_refold" not in ALL_MECHANISM_FAMILIES
    assert "definitional_unfold_refold" not in {m.family for m in PRESERVING_MECHANISMS}


def test_parallel_root_state_machine_accepts_higher_concurrency(tmp_path: Path) -> None:
    from leanfaith.sft2a.parallel_rehearsal import ParallelRootStateMachine

    states = ParallelRootStateMachine(tmp_path / "root_state.jsonl", maximum_workers=8)
    assert states.maximum_workers == 8
    assert states.claim(root_id="root-a", worker_id="w-0") == "claimed"
    assert states.claim(root_id="root-b", worker_id="w-1") == "claimed"
    assert states.claim(root_id="root-c", worker_id="w-2") == "claimed"


def test_oracle_pool_starts_empty_and_closes_cleanly() -> None:
    from leanfaith.sft2a.provider_rehearsal_v52 import OraclePool

    pool = OraclePool(cache_version="v2", workers=2)
    assert pool.workers == 2
    assert pool.active_backend_count() == 0
    pool.close()
    assert pool.active_backend_count() == 0
    assert pool.stats["created_backends"] == 0
