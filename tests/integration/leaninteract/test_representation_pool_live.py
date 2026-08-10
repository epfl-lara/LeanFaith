"""Live proof that representation requests reach LeanServerPool concurrently."""

from __future__ import annotations

import datetime
import shutil
from pathlib import Path

import pytest

from leanfaith.config.paths import find_repo_root
from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend
from leanfaith.lean.session_policy import ServerMode
from leanfaith.representations import TheoremForRepresentation, build_representations
from leanfaith.schemas import ViewStatus, make_id

pytestmark = [
    pytest.mark.lean,
    pytest.mark.skipif(shutil.which("lake") is None, reason="Lean toolchain unavailable"),
]

_FIXTURES = find_repo_root(Path(__file__).parent) / "tests" / "lean_fixtures"
_CONTEXT_FINGERPRINT = "0" * 64
_CONTEXT_ID = f"ctx:{_CONTEXT_FINGERPRINT}"
_CREATED_AT = datetime.datetime(2026, 8, 10, tzinfo=datetime.UTC)


def test_two_representation_requests_complete_through_live_pool(tmp_path: Path) -> None:
    backend = LeanInteractBackend(
        BackendSettings(
            project_dir=_FIXTURES,
            context_fingerprint=_CONTEXT_FINGERPRINT,
            environment_schema_version=1,
            raw_response_dir=tmp_path / "raw",
            server_mode=ServerMode.POOL,
            workers=2,
            enable_parallel_elaboration=False,
        )
    )
    try:
        theorems = [
            TheoremForRepresentation(
                theorem_id=make_id("thm", {"representation_pool_live": index}),
                full_name=name,
                proof_stripped=code,
                context_id=_CONTEXT_ID,
            )
            for index, (name, code) in enumerate(
                (
                    (
                        "lf_add_comm",
                        "theorem lf_add_comm (x y : Nat) : x + y = y + x := by sorry",
                    ),
                    (
                        "lf_zero_add",
                        "theorem lf_zero_add (n : Nat) : 0 + n = n := by sorry",
                    ),
                )
            )
        ]
        records = build_representations(
            backend,
            theorems,
            imports="import LeanFaithFixtures",
            created_at=_CREATED_AT,
        )
    finally:
        backend.close()

    assert len(records) == 2
    assert all(record.view_status["signature_explicit"] == ViewStatus.OK for record in records)
    assert all(record.view_status["semantic_atoms"] == ViewStatus.OK for record in records)
