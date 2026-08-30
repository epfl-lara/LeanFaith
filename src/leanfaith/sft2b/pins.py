"""Runtime verification for the frozen REPR dependency and SFT2B helper."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import yaml

from leanfaith.config.hashing import hash_canonical, hash_file, sha256_hex
from leanfaith.representations import goal_v1

REPR_FREEZE_COMMIT = "176a783842c5a73b84413dfa8347670608b615d9"
REPR_SPEC_HASH = "68d893a2c566bf3f6a82c899a32a351f9a5420f5ea98168c99b887aaa01a45a8"
REPR_IMPLEMENTATION_SET_HASH = "9a9252fff5ffc69cb65e71120fedffa83ed47271aecadbecf0ceb890feea65ff"
REPR_API_HASH = "c695ad868c98f27218e82184559d90624491df25c7805bf29861dd891787261d"
REPR_CONFIG_RELATIVE = Path("configs/representations/goal_v1_v1.yaml")
REPR_CONFIG_FILE_HASH = "a65d5b29760bbc5eb89405927f946f205eb99856c0538fdf5b57d3f9eceb0db7"
REPR_NAMESPACE = "LeanFaith.GoalV1"
REPR_SIGNATURE = "renderClosedProp (e : Expr) : MetaM String"


class PinVerificationError(RuntimeError):
    """Raised before execution when any frozen dependency has drifted."""


@dataclass(frozen=True, slots=True)
class RuntimePins:
    repr_freeze_commit: str
    repr_spec_hash: str
    repr_implementation_set_hash: str
    repr_api_hash: str
    repr_config_file_hash: str
    lean_renderer_path: str
    lean_renderer_hash: str
    injected_helper_hash: str
    python_module_path: str
    python_module_hash: str
    renderer_semantic_hash: str
    universe_profile_id: str
    universe_profile_hash: str
    render_context_id: str
    render_context_hash: str
    coverage_receipt_hash: str
    sft2b_helper_path: str
    sft2b_helper_hash: str

    def to_dict(self) -> dict[str, str]:
        return {
            "repr_freeze_commit": self.repr_freeze_commit,
            "repr_spec_hash": self.repr_spec_hash,
            "repr_implementation_set_hash": self.repr_implementation_set_hash,
            "repr_api_hash": self.repr_api_hash,
            "repr_config_file_hash": self.repr_config_file_hash,
            "lean_renderer_path": self.lean_renderer_path,
            "lean_renderer_hash": self.lean_renderer_hash,
            "injected_helper_hash": self.injected_helper_hash,
            "python_module_path": self.python_module_path,
            "python_module_hash": self.python_module_hash,
            "renderer_semantic_hash": self.renderer_semantic_hash,
            "universe_profile_id": self.universe_profile_id,
            "universe_profile_hash": self.universe_profile_hash,
            "render_context_id": self.render_context_id,
            "render_context_hash": self.render_context_hash,
            "coverage_receipt_hash": self.coverage_receipt_hash,
            "sft2b_helper_path": self.sft2b_helper_path,
            "sft2b_helper_hash": self.sft2b_helper_hash,
        }


def _git_bytes(repo_root: Path, commit: str, relative: Path) -> bytes:
    completed = subprocess.run(
        ["git", "show", f"{commit}:{relative.as_posix()}"],
        cwd=repo_root,
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise PinVerificationError(f"cannot read frozen Git object {relative}: {detail}")
    return completed.stdout


def _expect(label: str, observed: object, expected: object) -> None:
    if observed != expected:
        raise PinVerificationError(f"{label} mismatch: expected {expected!r}, got {observed!r}")


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise PinVerificationError(f"{label} must be a string-keyed mapping")
    return cast(dict[str, Any], value)


def _strip_imports(source: str) -> str:
    return "\n".join(line for line in source.splitlines() if not line.startswith("import "))


def verify_runtime_pins(repo_root: Path, *, helper_path: Path) -> RuntimePins:
    """Verify every hash-bearing REPR field directly from its frozen config.

    This runs before journal/cache/model/Lean work. Both the working bytes and
    their corresponding bytes in the coherent freeze commit must agree.
    """

    config_path = repo_root / REPR_CONFIG_RELATIVE
    config_bytes = config_path.read_bytes()
    _expect("REPR config file hash", sha256_hex(config_bytes), REPR_CONFIG_FILE_HASH)
    _expect(
        "working REPR config versus coherent freeze",
        config_bytes,
        _git_bytes(repo_root, REPR_FREEZE_COMMIT, REPR_CONFIG_RELATIVE),
    )
    loaded = yaml.safe_load(config_bytes)
    config = _mapping(loaded, "REPR config")
    _expect("REPR config status", config.get("status"), "frozen")
    _expect("REPR spec hash field", config.get("spec_hash"), REPR_SPEC_HASH)
    spec = _mapping(config.get("spec"), "REPR spec")
    _expect("REPR canonical spec hash", hash_canonical(spec), REPR_SPEC_HASH)
    _expect("REPR Python SPEC_HASH", goal_v1.SPEC_HASH, REPR_SPEC_HASH)

    sources = _mapping(config.get("implementation_sources"), "implementation_sources")
    lean_source = _mapping(sources.get("lean_renderer"), "lean renderer source")
    python_source = _mapping(sources.get("python_module"), "Python renderer source")
    lean_path = Path(str(lean_source.get("path")))
    python_path = Path(str(python_source.get("path")))
    lean_hash = str(lean_source.get("sha256"))
    python_hash = str(python_source.get("sha256"))
    injected_hash = str(lean_source.get("injected_helper_sha256"))
    _expect("Lean renderer working hash", hash_file(repo_root / lean_path), lean_hash)
    _expect("Python renderer working hash", hash_file(repo_root / python_path), python_hash)
    _expect(
        "Lean renderer versus coherent freeze",
        (repo_root / lean_path).read_bytes(),
        _git_bytes(repo_root, REPR_FREEZE_COMMIT, lean_path),
    )
    _expect(
        "Python renderer versus coherent freeze",
        (repo_root / python_path).read_bytes(),
        _git_bytes(repo_root, REPR_FREEZE_COMMIT, python_path),
    )
    lean_text = (repo_root / lean_path).read_text(encoding="utf-8")
    _expect(
        "REPR injected-helper hash",
        sha256_hex(_strip_imports(lean_text).encode("utf-8")),
        injected_hash,
    )

    renderer_semantic_hash = str(spec.get("renderer_semantic_hash"))
    universe_profile_hash = str(spec.get("canonical_universe_profile_hash"))
    render_context_hash = str(spec.get("render_context_hash"))
    universe_profile = _mapping(spec.get("canonical_universe_profile"), "universe profile")
    render_context = _mapping(spec.get("render_context"), "render context")
    coverage = _mapping(spec.get("elaborated_post_validator_coverage"), "coverage receipt")
    _expect(
        "renderer semantic payload hash",
        hash_canonical(spec["renderer_semantic_contract"]),
        renderer_semantic_hash,
    )
    _expect(
        "universe profile payload hash",
        hash_canonical(universe_profile),
        universe_profile_hash,
    )
    _expect("render context payload hash", hash_canonical(render_context), render_context_hash)
    coverage_payload = dict(coverage)
    coverage_receipt_hash = str(coverage_payload.pop("receipt_hash"))
    _expect(
        "coverage receipt payload hash",
        hash_canonical(coverage_payload),
        coverage_receipt_hash,
    )
    _expect("Python renderer semantic hash", goal_v1.RENDERER_SEMANTIC_HASH, renderer_semantic_hash)
    _expect(
        "Python universe profile hash",
        goal_v1.CANONICAL_UNIVERSE_PROFILE_HASH,
        universe_profile_hash,
    )
    _expect("Python render context hash", goal_v1.RENDER_CONTEXT_HASH, render_context_hash)

    implementation_payload = {
        "renderer_semantic_hash": renderer_semantic_hash,
        "lean_renderer_sha256": lean_hash,
        "injected_helper_sha256": injected_hash,
        "python_module_sha256": python_hash,
        "config_file_sha256": REPR_CONFIG_FILE_HASH,
    }
    _expect(
        "REPR implementation-set hash",
        hash_canonical(implementation_payload),
        REPR_IMPLEMENTATION_SET_HASH,
    )
    api_payload = {
        "replacement_commit": REPR_FREEZE_COMMIT,
        "replacement_lean_renderer_path": lean_path.as_posix(),
        "replacement_lean_renderer_sha256": lean_hash,
        "required_namespace": REPR_NAMESPACE,
        "required_signature": REPR_SIGNATURE,
    }
    _expect("REPR API hash", hash_canonical(api_payload), REPR_API_HASH)

    helper_relative = helper_path.relative_to(repo_root)
    helper_hash = hash_file(helper_path)
    helper_config = json.loads((repo_root / "configs/sft2b/runtime_v1.json").read_text())
    if not isinstance(helper_config, dict):
        raise PinVerificationError("SFT2B runtime config must be an object")
    _expect("SFT2B helper path", helper_config.get("lean_helper_path"), helper_relative.as_posix())
    _expect("SFT2B helper hash", helper_config.get("lean_helper_sha256"), helper_hash)

    return RuntimePins(
        repr_freeze_commit=REPR_FREEZE_COMMIT,
        repr_spec_hash=REPR_SPEC_HASH,
        repr_implementation_set_hash=REPR_IMPLEMENTATION_SET_HASH,
        repr_api_hash=REPR_API_HASH,
        repr_config_file_hash=REPR_CONFIG_FILE_HASH,
        lean_renderer_path=lean_path.as_posix(),
        lean_renderer_hash=lean_hash,
        injected_helper_hash=injected_hash,
        python_module_path=python_path.as_posix(),
        python_module_hash=python_hash,
        renderer_semantic_hash=renderer_semantic_hash,
        universe_profile_id=str(universe_profile["profile_id"]),
        universe_profile_hash=universe_profile_hash,
        render_context_id=str(render_context["context_id"]),
        render_context_hash=render_context_hash,
        coverage_receipt_hash=coverage_receipt_hash,
        sft2b_helper_path=helper_relative.as_posix(),
        sft2b_helper_hash=helper_hash,
    )
