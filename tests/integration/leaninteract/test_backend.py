"""LF-008 integration: the LeanInteract adapter against the fixture project (§8.12)."""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from leanfaith.config.paths import find_repo_root
from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend
from leanfaith.lean.protocol import LeanRequest, LeanStatus, RequestValidationError

pytestmark = [
    pytest.mark.lean,
    pytest.mark.skipif(shutil.which("lake") is None, reason="Lean toolchain unavailable"),
]

_REPO_ROOT = find_repo_root(Path(__file__).parent)
_FIXTURES = _REPO_ROOT / "tests" / "lean_fixtures"
_CTX_FINGERPRINT = "0" * 64
_CTX = f"ctx:{_CTX_FINGERPRINT}"


@pytest.fixture(scope="module")
def backend(tmp_path_factory: pytest.TempPathFactory) -> Iterator[LeanInteractBackend]:
    raw_dir = tmp_path_factory.mktemp("raw_lean_responses")
    instance = LeanInteractBackend(
        BackendSettings(
            project_dir=_FIXTURES,
            context_fingerprint=_CTX_FINGERPRINT,
            environment_schema_version=1,
            raw_response_dir=raw_dir,
        )
    )
    yield instance
    instance.close()


def _request(request_id: str, **overrides: object) -> LeanRequest:
    payload: dict[str, object] = {"request_id": request_id, "context_id": _CTX}
    payload.update(overrides)
    return LeanRequest(**payload)  # type: ignore[arg-type]


def test_valid_command(backend: LeanInteractBackend) -> None:
    result = backend.run(
        _request("valid", code="theorem it_add (x y : Nat) : x + y = y + x := by omega")
    )
    assert result.status == LeanStatus.VALID
    assert result.messages == ()
    assert result.request_id == "valid"


def test_placeholder_command(backend: LeanInteractBackend) -> None:
    result = backend.run(
        _request(
            "placeholder",
            code="theorem it_sorry (n : Nat) : n + 0 = n := sorry",
            allow_sorry=True,
        )
    )
    assert result.status == LeanStatus.VALID_WITH_SORRY
    assert len(result.sorries) == 1


def test_invalid_command_preserves_diagnostics(backend: LeanInteractBackend) -> None:
    result = backend.run(_request("invalid", code="theorem it_bad (n : Nat) : n + 1 = n := rfl"))
    assert result.status == LeanStatus.INVALID
    assert any(message["severity"] == "error" for message in result.messages)


def test_root_goals_extracted_for_proved_code(backend: LeanInteractBackend) -> None:
    result = backend.run(
        _request(
            "root-goals",
            code="theorem it_rg (x y : Nat) : x + y = y + x := by omega",
            root_goals=True,
        )
    )
    assert result.status == LeanStatus.VALID
    assert len(result.root_goals) == 1
    assert "x + y = y + x" in result.root_goals[0]
    assert result.sorries == ()


def test_command_declarations_golden(backend: LeanInteractBackend) -> None:
    result = backend.run(
        _request(
            "decls",
            code="theorem it_decl (n : Nat) : n = n := rfl",
            declarations=True,
        )
    )
    assert result.status == LeanStatus.VALID
    assert [d["name"] for d in result.declarations] == ["it_decl"]
    declaration = result.declarations[0]
    golden_path = _REPO_ROOT / "tests" / "golden" / "leaninteract" / "command_declaration.json"
    golden = json.loads(golden_path.read_text(encoding="utf-8"))
    observed = {
        "name": declaration["name"],
        "full_name": declaration["full_name"],
        "kind": declaration["kind"],
        "signature_pp": declaration["signature"]["pp"],
        "binder_ids": [b["id"] for b in declaration["binders"]["map"]],
    }
    assert observed == golden


def test_file_command_declarations(backend: LeanInteractBackend) -> None:
    result = backend.run(
        _request("file", file_path=Path("LeanFaithFixtures/Basic.lean"), declarations=True)
    )
    assert result.status == LeanStatus.VALID
    assert [d["name"] for d in result.declarations] == [
        "lf_add_comm",
        "lf_zero_add",
        "lf_trivial",
        "lf_proof_body_a",
        "lf_proof_body_b",
        "LeanFaithLF021OfflineToken",
        "hidden",
    ]


def test_file_command_invalid_file(backend: LeanInteractBackend) -> None:
    result = backend.run(_request("file-bad", file_path=Path("Extra/Invalid.lean")))
    assert result.status == LeanStatus.INVALID


def test_timeout_then_recovery(backend: LeanInteractBackend) -> None:
    result = backend.run(
        _request(
            "timeout",
            code="theorem it_slow (x y : Nat) : x + y = y + x := by omega",
            timeout_seconds=0.001,
        )
    )
    assert result.status == LeanStatus.TIMEOUT
    assert result.infrastructure_error is not None
    # The pinned server is killed on timeout; the next request must recover.
    follow_up = backend.run(_request("recovery", code="theorem it_rec : True := trivial"))
    assert follow_up.status == LeanStatus.VALID


def test_batch_preserves_order_and_isolates_items(backend: LeanInteractBackend) -> None:
    requests = [
        _request("b-valid", code="theorem b1 : True := trivial"),
        _request("b-invalid", code="theorem b2 : False := trivial"),
        _request("b-sorry", code="theorem b3 : True := sorry", allow_sorry=True),
    ]
    results = backend.run_batch(requests)
    assert [r.request_id for r in results] == ["b-valid", "b-invalid", "b-sorry"]
    assert [r.status for r in results] == [
        LeanStatus.VALID,
        LeanStatus.INVALID,
        LeanStatus.VALID_WITH_SORRY,
    ]


def test_raw_response_persisted_before_normalization(backend: LeanInteractBackend) -> None:
    result = backend.run(_request("raw", code="theorem it_raw : True := trivial"))
    assert result.raw_response_path is not None
    payload = json.loads(Path(result.raw_response_path).read_text(encoding="utf-8"))
    assert payload["request_hash"] == result.request_hash
    assert payload["response"] is not None
    assert payload["request"]["code"] == "theorem it_raw : True := trivial"


def test_request_hash_deterministic_and_allow_sorry_sensitive(
    backend: LeanInteractBackend,
) -> None:
    first = backend.run(_request("h1", code="theorem it_h : True := trivial"))
    second = backend.run(_request("h2", code="theorem it_h : True := trivial"))
    assert first.request_hash == second.request_hash  # request_id excluded (§16.9)
    third = backend.run(_request("h3", code="theorem it_h : True := trivial", allow_sorry=True))
    assert third.request_hash != first.request_hash


def test_invalid_request_rejected_before_lean(backend: LeanInteractBackend) -> None:
    with pytest.raises(RequestValidationError):
        backend.run(_request("neither"))
