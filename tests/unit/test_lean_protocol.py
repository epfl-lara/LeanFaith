"""LF-005: Appendix A.5 backend protocol — plan parity, contract fidelity, invariants."""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

import pytest

from leanfaith.config.paths import find_repo_root
from leanfaith.lean import protocol
from leanfaith.lean.protocol import (
    LeanRequest,
    LeanStatus,
    RequestValidationError,
    compute_request_hash,
    validate_request,
)

_REPO_ROOT = find_repo_root(Path(__file__).parent)
# The A.5 backend-protocol contract lives in the frozen pre-refocus plan;
# it remains the spec the live protocol module must match.
_PLAN = (_REPO_ROOT / "docs" / "archive" / "PLAN-2026-08-frozen.md").read_text(encoding="utf-8")

_CANONICAL_CLASS_NAMES = ("LeanStatus", "LeanRequest", "LeanResult", "LeanBackend")


def _python_block_after(heading: str) -> str:
    """Extract the first ```python fence following a heading in the frozen plan."""
    heading_index = _PLAN.index(heading)
    match = re.search(r"```python\n(.*?)```", _PLAN[heading_index:], flags=re.DOTALL)
    assert match is not None, f"no python block after {heading!r}"
    return match.group(1)


def test_plan_8_4_and_a5_blocks_byte_equivalent() -> None:
    mirror = _python_block_after("### 8.4 Canonical backend protocol mirror")
    canonical = _python_block_after("### A.5 Canonical LeanFaith backend protocol")
    assert mirror == canonical, "frozen plan §8.4 must mirror Appendix A.5 byte-for-byte"


def _classes_by_name(source: str) -> dict[str, ast.ClassDef]:
    tree = ast.parse(source)
    return {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}


def test_protocol_module_matches_a5_contract() -> None:
    canonical = _classes_by_name(
        _python_block_after("### A.5 Canonical LeanFaith backend protocol")
    )
    implemented = _classes_by_name(inspect.getsource(protocol))
    assert set(canonical) == set(_CANONICAL_CLASS_NAMES)
    for name in _CANONICAL_CLASS_NAMES:
        assert name in implemented, f"protocol.py is missing {name}"
        assert ast.dump(canonical[name]) == ast.dump(implemented[name]), (
            f"{name} in protocol.py deviates from the Appendix A.5 contract"
        )


def test_no_leaninteract_import_outside_backend() -> None:
    """§8.12 item 12: only the adapter module may import LeanInteract."""
    allowed = Path("src/leanfaith/lean/leaninteract_backend.py")
    pattern = re.compile(r"^\s*(?:from|import)\s+lean_interact\b", flags=re.MULTILINE)
    offenders = [
        path.relative_to(_REPO_ROOT)
        for path in (_REPO_ROOT / "src" / "leanfaith").rglob("*.py")
        if path.relative_to(_REPO_ROOT) != allowed
        and pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], f"LeanInteract imported outside the backend: {offenders}"


def test_lean_status_matches_8_6_table() -> None:
    assert {status.value for status in LeanStatus} == {
        "valid",
        "valid_with_sorry",
        "invalid",
        "timeout",
        "crash",
        "setup_error",
        "unsupported",
        "internal_error",
    }


def _request(**overrides: object) -> LeanRequest:
    payload: dict[str, object] = {
        "request_id": "req-1",
        "context_id": "ctx:" + "0" * 64,
        "code": "theorem t : True := trivial",
    }
    payload.update(overrides)
    return LeanRequest(**payload)  # type: ignore[arg-type]


def test_validate_request_accepts_code_or_file() -> None:
    validate_request(_request())
    validate_request(_request(code=None, file_path=Path("Fixtures/Basic.lean")))


def test_validate_request_rejects_both_payloads() -> None:
    with pytest.raises(RequestValidationError, match="exactly one"):
        validate_request(_request(file_path=Path("Fixtures/Basic.lean")))


def test_validate_request_rejects_neither_payload() -> None:
    with pytest.raises(RequestValidationError, match="exactly one"):
        validate_request(_request(code=None))


def test_validate_request_rejects_bad_timeout() -> None:
    with pytest.raises(RequestValidationError, match="timeout"):
        validate_request(_request(timeout_seconds=0.0))


def test_validate_request_rejects_empty_ids() -> None:
    with pytest.raises(RequestValidationError, match="request_id"):
        validate_request(_request(request_id=""))
    with pytest.raises(RequestValidationError, match="context_id"):
        validate_request(_request(context_id=""))


def _hash(request: LeanRequest, **overrides: object) -> str:
    kwargs: dict[str, object] = {
        "context_fingerprint": "0" * 64,
        "environment_schema_version": 1,
        "method_version": "backend_v1",
    }
    kwargs.update(overrides)
    return compute_request_hash(request, **kwargs)  # type: ignore[arg-type]


def test_request_hash_deterministic() -> None:
    assert _hash(_request()) == _hash(_request())


def test_request_hash_ignores_request_id_and_metadata() -> None:
    base = _hash(_request())
    assert _hash(_request(request_id="req-2")) == base
    assert _hash(_request(metadata={"shard": "7"})) == base


@pytest.mark.parametrize(
    "override",
    [
        {"code": "theorem t : True := by trivial"},
        {"allow_sorry": True},
        {"timeout_seconds": 60.0},
        {"infotree": "substantive"},
        {"declarations": True},
        {"root_goals": True},
    ],
)
def test_request_hash_covers_request_fields(override: dict[str, object]) -> None:
    assert _hash(_request(**override)) != _hash(_request())


@pytest.mark.parametrize(
    "override",
    [
        {"environment_schema_version": 2},
        {"method_version": "backend_v2"},
    ],
)
def test_request_hash_covers_environment_fields(override: dict[str, object]) -> None:
    assert _hash(_request(), **override) != _hash(_request())


def test_request_hash_covers_context() -> None:
    base = _hash(_request())
    other_context = _hash(_request(context_id="ctx:" + "1" * 64), context_fingerprint="1" * 64)
    assert other_context != base


def test_request_hash_binds_context_id_to_fingerprint() -> None:
    # §8.11: a result can never be labeled with a context it did not run in.
    with pytest.raises(RequestValidationError, match="context fingerprint"):
        _hash(_request(context_id="ctx:" + "1" * 64))


def test_file_requests_require_content_hash() -> None:
    file_request = _request(code=None, file_path=Path("Fixtures/Basic.lean"))
    with pytest.raises(RequestValidationError, match="file_content_hash"):
        _hash(file_request)
    hashed = _hash(file_request, file_content_hash="a" * 64)
    assert hashed != _hash(file_request, file_content_hash="b" * 64)


def test_code_requests_reject_content_hash() -> None:
    with pytest.raises(RequestValidationError, match="file_content_hash"):
        _hash(_request(), file_content_hash="a" * 64)


def test_request_hash_validates_first() -> None:
    with pytest.raises(RequestValidationError):
        _hash(_request(code=None))


def test_request_and_result_are_frozen() -> None:
    request = _request()
    with pytest.raises(AttributeError):
        request.code = "changed"  # type: ignore[misc]
