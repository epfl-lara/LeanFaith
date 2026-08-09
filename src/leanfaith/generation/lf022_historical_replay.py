"""Isolated historical-code replay for frozen LF-022 execution batches.

The coordinator in this module may run from a newer LeanFaith checkout.  It
never imports that newer executor into the replay process.  Instead it
materializes the admission-bound code bundle as a private clean checkout,
copies only hash-bound runtime inputs plus exact task directories, and launches
the same Python interpreter with the historical ``src`` as its sole
``PYTHONPATH`` entry.  Provider credentials are absent and every executor call
uses its offline mode.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, Self, TypeGuard

from pydantic import Field, model_validator

from leanfaith.config.code_bundle import materialize_code_bundle_checkout
from leanfaith.config.hashing import canonical_json_bytes, hash_file
from leanfaith.config.models import StrictModel
from leanfaith.generation.lf022_batch import VerifiedLF022BatchTask
from leanfaith.generation.lf022_production import LF022ArtifactBinding
from leanfaith.schemas.ids import HEX64_PATTERN, id_pattern

_ARTIFACT_BINDING_CLOSURE_LIMIT = 100_000


class LF022HistoricalReplayError(RuntimeError):
    """The admission-bound historical replay could not be established exactly."""


class LF022HistoricalModuleBinding(StrictModel):
    """One required historical module loaded only from the admitted checkout."""

    module_name: str = Field(min_length=1)
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=HEX64_PATTERN)


class LF022HistoricalTerminalBinding(StrictModel):
    """One terminal reconstructed by the historical executor."""

    execution_task_id: str = Field(pattern=id_pattern("lf022_execution_task"))
    terminal_id: str = Field(pattern=id_pattern("lf022_execution_terminal"))
    terminal_artifact: LF022ArtifactBinding


class LF022HistoricalReplayResult(StrictModel):
    """Canonical stdout contract returned by the isolated replay process."""

    schema_version: Literal[1] = 1
    code_tree_hash: str = Field(pattern=HEX64_PATTERN)
    code_bundle_sha256: str = Field(pattern=HEX64_PATTERN)
    network_calls_performed: Literal[0] = 0
    module_bindings: tuple[LF022HistoricalModuleBinding, ...] = Field(min_length=1)
    terminal_bindings: tuple[LF022HistoricalTerminalBinding, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _sorted_unique(self) -> Self:
        module_names = tuple(item.module_name for item in self.module_bindings)
        if module_names != tuple(sorted(set(module_names))):
            raise ValueError("historical module bindings must be name-sorted and unique")
        task_ids = tuple(item.execution_task_id for item in self.terminal_bindings)
        if task_ids != tuple(sorted(set(task_ids))):
            raise ValueError("historical terminal bindings must be task-sorted and unique")
        terminal_ids = tuple(item.terminal_id for item in self.terminal_bindings)
        terminal_paths = tuple(item.terminal_artifact.path for item in self.terminal_bindings)
        if len(set(terminal_ids)) != len(terminal_ids):
            raise ValueError("historical terminal bindings contain duplicate terminal IDs")
        if len(set(terminal_paths)) != len(terminal_paths):
            raise ValueError("historical terminal bindings contain duplicate terminal paths")
        return self


_HISTORICAL_BOOTSTRAP = r"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def canonical(value: object) -> bytes:
    text = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return (text + "\n").encode("utf-8")


payload = json.loads(sys.stdin.buffer.read())
root = Path(payload["historical_root"]).resolve(strict=True)
source = (root / "src").resolve(strict=True)
forbidden = Path(payload["forbidden_current_root"]).resolve(strict=True)
forbidden_source = (forbidden / "src").resolve(strict=False)

# Editable installs can add the current checkout through a .pth file even when
# PYTHONPATH is replaced.  Remove that path before importing LeanFaith, while
# retaining ordinary site-packages needed by the admitted source.
clean_path: list[str] = [str(source)]
for entry in sys.path:
    if not entry:
        continue
    resolved = Path(entry).resolve(strict=False)
    if resolved == source:
        continue
    # Reject the editable checkout entries themselves, but retain ordinary
    # runtime dependencies from a project-local virtual environment.  Every
    # loaded ``leanfaith.*`` module is origin-checked below, so a dependency
    # path cannot silently supply current executor code.
    if resolved == forbidden or resolved == forbidden_source:
        continue
    clean_path.append(str(resolved))
sys.path[:] = clean_path
if os.environ.get("PYTHONPATH") != str(source):
    raise RuntimeError("historical replay PYTHONPATH is not the admitted source only")
credential_markers = ("API_KEY", "ACCESS_TOKEN", "AUTH_TOKEN", "PASSWORD", "SECRET", "CREDENTIAL")
credential_prefixes = ("RCP_", "OPENAI_", "ANTHROPIC_", "MOONSHOT_", "QWEN_", "ZAI_", "GLM_")
leaked = sorted(
    key
    for key in os.environ
    if key.upper().startswith(credential_prefixes)
    or any(marker in key.upper() for marker in credential_markers)
)
if leaked:
    raise RuntimeError(f"provider credential variables reached historical replay: {leaked!r}")

import leanfaith.config.hashing as hashing_module
import leanfaith.generation.lf022_batch as batch_module
import leanfaith.generation.lf022_executor as executor_module
import leanfaith.generation.lf022_production as production_module
import leanfaith.generation.llm_variants as variants_module
import leanfaith.schemas.manifest as manifest_module

required_modules = (
    hashing_module,
    batch_module,
    executor_module,
    production_module,
    variants_module,
    manifest_module,
)


def loaded_leanfaith_module_bindings() -> list[dict[str, str]]:
    bindings: list[dict[str, str]] = []
    for name, module in sorted(tuple(sys.modules.items())):
        if name != "leanfaith" and not name.startswith("leanfaith."):
            continue
        module_file = getattr(module, "__file__", None)
        if module_file is None:
            continue
        resolved = Path(module_file).resolve(strict=True)
        if resolved != source and source not in resolved.parents:
            raise RuntimeError(
                f"historical module leaked from outside admitted source: {name}={resolved}"
            )
        bindings.append(
            {
                "module_name": name,
                "path": resolved.relative_to(root).as_posix(),
                "sha256": hashing_module.hash_file(resolved),
            }
        )
    return bindings


# Validate the eager import closure before any executor call.  Repeat this after
# replay so modules imported lazily by historical code cannot evade the origin
# boundary or the returned content bindings.
loaded_leanfaith_module_bindings()

state = manifest_module.collect_code_state(root)
if state.code_tree_hash != payload["expected_code_tree_hash"] or state.git_dirty:
    raise RuntimeError("historical checkout does not reproduce the admitted clean code state")

binding = production_module.LF022ArtifactBinding.model_validate(payload["manifest_binding"])
loader = getattr(batch_module, "load_lf022_public_batch", None)
if loader is None:
    loader = getattr(batch_module, "_load_batch")
manifest, loaded_tasks = loader(repo_root=root, manifest_binding=binding)
if len(loaded_tasks) != payload["expected_task_count"]:
    raise RuntimeError("historical batch loader returned the wrong task count")

terminal_bindings: list[dict[str, object]] = []
for loaded in loaded_tasks:
    result = executor_module.execute_lf022_g_open_task(
        repo_root=root,
        output_root=root / manifest.executor_output_root,
        admission=loaded.admission,
        task=loaded.task,
        execute_public_provisional=False,
        verified_admission=loaded.verified,
        verified_task_inputs=loaded.task_inputs,
        observed_code_tree_hash=state.code_tree_hash,
    )
    if (
        not result.replayed
        or result.network_calls_this_run != 0
        or result.terminal is None
        or result.terminal_path is None
    ):
        raise RuntimeError("historical executor did not return one exact offline terminal replay")
    terminal_path = result.terminal_path.resolve(strict=True)
    relative = terminal_path.relative_to(root).as_posix()
    terminal_bindings.append(
        {
            "execution_task_id": loaded.task.execution_task_id,
            "terminal_id": result.terminal.terminal_id,
            "terminal_artifact": {
                "path": relative,
                "sha256": hashing_module.hash_file(terminal_path),
            },
        }
    )

module_bindings = loaded_leanfaith_module_bindings()
output = {
    "schema_version": 1,
    "code_tree_hash": state.code_tree_hash,
    "code_bundle_sha256": payload["code_bundle_sha256"],
    "network_calls_performed": 0,
    "module_bindings": module_bindings,
    "terminal_bindings": sorted(terminal_bindings, key=lambda item: item["execution_task_id"]),
}
sys.stdout.buffer.write(canonical(output))
"""


def _safe_relative(value: str, *, label: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value.strip()
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or "\\" in value
        or path.as_posix() != value
    ):
        raise LF022HistoricalReplayError(f"{label} is not a safe repository-relative path")
    return path


def _source_file(root: Path, relative: PurePosixPath, *, label: str) -> Path:
    current = root.resolve(strict=True)
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise LF022HistoricalReplayError(f"{label} contains a symlinked component")
    if not current.is_file():
        raise LF022HistoricalReplayError(f"{label} is missing or not a regular file")
    return current


def _copy_exact_file(
    *,
    source_root: Path,
    historical_root: Path,
    relative: PurePosixPath,
    expected_sha256: str,
    label: str,
) -> Path:
    source = _source_file(source_root, relative, label=label)
    if hash_file(source) != expected_sha256:
        raise LF022HistoricalReplayError(f"{label} differs from its SHA-256 binding")
    destination = historical_root / Path(relative.as_posix())
    current = historical_root.resolve(strict=True)
    for part in relative.parts[:-1]:
        current /= part
        if current.is_symlink():
            raise LF022HistoricalReplayError(f"historical {label} parent is a symlink")
        current.mkdir(exist_ok=True)
    if destination.exists():
        if destination.is_symlink() or not destination.is_file():
            raise LF022HistoricalReplayError(f"historical {label} is not a regular file")
        if destination.read_bytes() != source.read_bytes():
            raise LF022HistoricalReplayError(f"historical {label} conflicts with bundled bytes")
    else:
        shutil.copyfile(source, destination)
        destination.chmod(0o600)
    if hash_file(destination) != expected_sha256:
        raise LF022HistoricalReplayError(f"historical {label} copy hash drifted")
    return destination


def _is_sha256(value: object) -> TypeGuard[str]:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


@dataclass(frozen=True)
class _DiscoveredBinding:
    path: str
    sha256: str
    follow_bindings: bool


def _is_identifier(value: object, prefix: str) -> bool:
    return (
        isinstance(value, str)
        and value.startswith(f"{prefix}:")
        and _is_sha256(value.removeprefix(f"{prefix}:"))
    )


def _record_binding(
    value: dict[object, object],
    *,
    path_key: str,
    digest_key: str,
    required: bool,
    follow_bindings: bool,
) -> list[_DiscoveredBinding]:
    path = value.get(path_key)
    digest = value.get(digest_key)
    if not required and path is None and digest is None:
        return []
    if not isinstance(path, str) or not path or not _is_sha256(digest):
        raise LF022HistoricalReplayError(
            f"historical record has incomplete or invalid {path_key}/{digest_key} binding"
        )
    return [_DiscoveredBinding(path, digest, follow_bindings)]


def _record_binding_list(
    value: dict[object, object],
    *,
    paths_key: str,
    digests_key: str,
) -> list[_DiscoveredBinding]:
    paths = value.get(paths_key)
    digests = value.get(digests_key)
    if (
        not isinstance(paths, list)
        or not paths
        or not isinstance(digests, list)
        or len(paths) != len(digests)
    ):
        raise LF022HistoricalReplayError(
            f"historical record has incomplete or invalid {paths_key}/{digests_key} bindings"
        )
    result: list[_DiscoveredBinding] = []
    for path, digest in zip(paths, digests, strict=True):
        if not isinstance(path, str) or not path or not _is_sha256(digest):
            raise LF022HistoricalReplayError(
                f"historical record has incomplete or invalid {paths_key}/{digests_key} bindings"
            )
        result.append(_DiscoveredBinding(path, digest, True))
    return result


def _explicit_record_bindings(
    value: dict[object, object],
) -> list[_DiscoveredBinding] | None:
    if _is_identifier(value.get("terminal_id"), "lf022_execution_terminal"):
        terminal_bindings = _record_binding_list(
            value,
            paths_key="attempt_artifacts",
            digests_key="attempt_sha256s",
        )
        terminal_bindings.extend(
            _record_binding_list(
                value,
                paths_key="llm_attempt_artifacts",
                digests_key="llm_attempt_sha256s",
            )
        )
        terminal_bindings.extend(
            _record_binding(
                value,
                path_key="llm_call_artifact",
                digest_key="llm_call_sha256",
                required=True,
                follow_bindings=True,
            )
        )
        terminal_bindings.extend(
            _record_binding(
                value,
                path_key="variants_artifact",
                digest_key="variants_sha256",
                required=False,
                follow_bindings=False,
            )
        )
        return terminal_bindings

    if (
        _is_identifier(value.get("execution_task_id"), "lf022_execution_task")
        and _is_identifier(value.get("provider_attempt_id"), "provider-attempt")
        and isinstance(value.get("attempt_index"), int)
        and not isinstance(value.get("attempt_index"), bool)
    ):
        execution_attempt_bindings: list[_DiscoveredBinding] = []
        for path_key, digest_key in (
            ("request_artifact", "request_sha256"),
            ("wire_request_artifact", "wire_request_sha256"),
            ("provider_raw_artifact", "provider_raw_sha256"),
        ):
            execution_attempt_bindings.extend(
                _record_binding(
                    value,
                    path_key=path_key,
                    digest_key=digest_key,
                    required=True,
                    follow_bindings=False,
                )
            )
        response_keys = (
            "wire_response_body_artifact",
            "wire_response_body_sha256",
            "wire_response_metadata_artifact",
            "wire_response_metadata_sha256",
        )
        response_values = tuple(value.get(key) for key in response_keys)
        if any(item is not None for item in response_values):
            if any(item is None for item in response_values):
                raise LF022HistoricalReplayError(
                    "historical execution attempt has an incomplete wire response binding"
                )
            execution_attempt_bindings.extend(
                _record_binding(
                    value,
                    path_key="wire_response_body_artifact",
                    digest_key="wire_response_body_sha256",
                    required=True,
                    follow_bindings=False,
                )
            )
            execution_attempt_bindings.extend(
                _record_binding(
                    value,
                    path_key="wire_response_metadata_artifact",
                    digest_key="wire_response_metadata_sha256",
                    required=True,
                    follow_bindings=False,
                )
            )
        return execution_attempt_bindings

    if _is_identifier(value.get("attempt_id"), "call_attempt") and _is_identifier(
        value.get("call_id"), "call"
    ):
        llm_attempt_bindings = _record_binding(
            value,
            path_key="request_artifact",
            digest_key="request_artifact_sha256",
            required=False,
            follow_bindings=False,
        )
        llm_attempt_bindings.extend(
            _record_binding(
                value,
                path_key="raw_response_artifact",
                digest_key="raw_response_sha256",
                required=False,
                follow_bindings=False,
            )
        )
        return llm_attempt_bindings

    if _is_identifier(value.get("call_id"), "call") and "attempt_id" not in value:
        llm_call_bindings = _record_binding(
            value,
            path_key="request_artifact",
            digest_key="request_artifact_sha256",
            required=False,
            follow_bindings=False,
        )
        llm_call_bindings.extend(
            _record_binding(
                value,
                path_key="raw_output_artifact",
                digest_key="raw_response_sha256",
                required=False,
                follow_bindings=False,
            )
        )
        return llm_call_bindings
    return None


def _binding_candidates_with_policy(value: object) -> list[_DiscoveredBinding]:
    result: list[_DiscoveredBinding] = []
    if isinstance(value, dict):
        explicit = _explicit_record_bindings(value)
        if explicit is not None:
            return explicit
        path = value.get("path")
        digest = value.get("sha256")
        if isinstance(path, str) and _is_sha256(digest):
            result.append(_DiscoveredBinding(path, digest, True))
        for child in value.values():
            result.extend(_binding_candidates_with_policy(child))
    elif isinstance(value, list):
        for child in value:
            result.extend(_binding_candidates_with_policy(child))
    return result


def _binding_candidates(value: object) -> list[tuple[str, str]]:
    return [(binding.path, binding.sha256) for binding in _binding_candidates_with_policy(value)]


def _json_values(path: Path) -> Iterator[object]:
    if path.suffix not in {".json", ".jsonl"}:
        return
    try:
        if path.suffix == ".jsonl":
            with path.open(encoding="utf-8") as stream:
                for line in stream:
                    if line.strip():
                        yield json.loads(line)
            return
        with path.open(encoding="utf-8") as stream:
            yield json.load(stream)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise LF022HistoricalReplayError(f"bound JSON artifact is invalid: {path}") from exc


def _copy_binding_closure(
    *,
    source_root: Path,
    historical_root: Path,
    initial: tuple[LF022ArtifactBinding, ...],
) -> None:
    queue: list[tuple[str, str, bool]] = []
    discovered: dict[str, str] = {}
    binding_scans_requested: set[str] = set()
    binding_scans_completed: set[str] = set()

    def enqueue(relative_text: str, digest: str, *, follow_bindings: bool) -> None:
        prior = discovered.get(relative_text)
        if prior is not None:
            if prior != digest:
                raise LF022HistoricalReplayError(
                    f"artifact closure has conflicting hashes for {relative_text}"
                )
            if follow_bindings and relative_text not in binding_scans_requested:
                binding_scans_requested.add(relative_text)
                queue.append((relative_text, digest, True))
            return
        if len(discovered) >= _ARTIFACT_BINDING_CLOSURE_LIMIT:
            raise LF022HistoricalReplayError("artifact binding closure exceeds safety limit")
        discovered[relative_text] = digest
        if follow_bindings:
            binding_scans_requested.add(relative_text)
        queue.append((relative_text, digest, follow_bindings))

    for binding in initial:
        enqueue(binding.path, binding.sha256, follow_bindings=True)

    while queue:
        relative_text, digest, follow_bindings = queue.pop()
        relative = _safe_relative(relative_text, label="bound artifact")
        copied = _copy_exact_file(
            source_root=source_root,
            historical_root=historical_root,
            relative=relative,
            expected_sha256=digest,
            label="bound artifact",
        )
        if not follow_bindings or relative_text in binding_scans_completed:
            continue
        binding_scans_completed.add(relative_text)
        for value in _json_values(copied):
            for child in _binding_candidates_with_policy(value):
                enqueue(
                    child.path,
                    child.sha256,
                    follow_bindings=child.follow_bindings,
                )


def _copy_task_directory(
    *,
    source_root: Path,
    historical_root: Path,
    executor_output_root: str,
    execution_task_id: str,
) -> None:
    digest = execution_task_id.removeprefix("lf022_execution_task:")
    relative = _safe_relative(
        f"{executor_output_root}/tasks/{digest[:2]}/{digest}",
        label="executor task directory",
    )
    source = source_root / Path(relative.as_posix())
    if source.is_symlink() or not source.is_dir():
        raise LF022HistoricalReplayError("executor task directory is missing or unsafe")
    destination = historical_root / Path(relative.as_posix())
    if destination.exists():
        raise LF022HistoricalReplayError("executor task directory collides with code bundle")
    for item in source.rglob("*"):
        if item.is_symlink():
            raise LF022HistoricalReplayError("executor task directory contains a symlink")
        if not item.is_file() and not item.is_dir():
            raise LF022HistoricalReplayError("executor task directory contains a special file")
    shutil.copytree(source, destination, symlinks=False)


def _credential_free_environment(historical_source: Path) -> dict[str, str]:
    private_home = historical_source.parent.parent / "home"
    private_home.mkdir(mode=0o700, exist_ok=True)
    if private_home.is_symlink() or private_home.stat().st_mode & 0o077:
        raise LF022HistoricalReplayError("historical replay HOME is not private")
    allowed = {
        "LANG",
        "LC_ALL",
        "LD_LIBRARY_PATH",
        "PATH",
        "SSL_CERT_FILE",
        "TZ",
        "VIRTUAL_ENV",
    }
    environment = {key: value for key, value in os.environ.items() if key in allowed}
    environment.update(
        {
            "HOME": str(private_home),
            "PYTHONPATH": str(historical_source),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONSAFEPATH": "1",
        }
    )
    return environment


def _launch_historical_subprocess(
    *,
    historical_root: Path,
    current_root: Path,
    manifest_binding: LF022ArtifactBinding,
    code_tree_hash: str,
    code_bundle_sha256: str,
    task_count: int,
) -> LF022HistoricalReplayResult:
    payload = {
        "historical_root": str(historical_root),
        "forbidden_current_root": str(current_root.resolve(strict=True)),
        "manifest_binding": manifest_binding.model_dump(mode="json"),
        "expected_code_tree_hash": code_tree_hash,
        "code_bundle_sha256": code_bundle_sha256,
        "expected_task_count": task_count,
    }
    completed = subprocess.run(
        [sys.executable, "-P", "-c", _HISTORICAL_BOOTSTRAP],
        cwd=historical_root,
        env=_credential_free_environment(historical_root / "src"),
        input=canonical_json_bytes(payload),
        capture_output=True,
        check=False,
        timeout=600,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace")[-4000:]
        raise LF022HistoricalReplayError(
            f"isolated historical replay failed with code {completed.returncode}: {stderr}"
        )
    try:
        result = LF022HistoricalReplayResult.model_validate_json(completed.stdout)
    except ValueError as exc:
        raise LF022HistoricalReplayError("historical replay returned invalid JSON") from exc
    expected = canonical_json_bytes(result.model_dump(mode="json")) + b"\n"
    if completed.stdout != expected:
        raise LF022HistoricalReplayError("historical replay stdout is not canonical JSON")
    return result


def run_lf022_historical_replay(
    *,
    repo_root: Path,
    manifest_binding: LF022ArtifactBinding,
    loaded_tasks: tuple[VerifiedLF022BatchTask, ...],
    executor_output_root: str,
) -> LF022HistoricalReplayResult:
    """Replay one frozen batch solely with its admission-bound historical code."""

    if not loaded_tasks:
        raise LF022HistoricalReplayError("historical replay requires at least one frozen task")
    admissions = {task.admission.admission_id: task.admission for task in loaded_tasks}
    code_tree_hashes = {admission.code_tree_hash for admission in admissions.values()}
    code_bundles = {
        (
            admission.artifacts.code_bundle.path,
            admission.artifacts.code_bundle.sha256,
        )
        for admission in admissions.values()
    }
    if len(code_tree_hashes) != 1 or len(code_bundles) != 1:
        raise LF022HistoricalReplayError(
            "one QA batch must bind exactly one historical code tree and bundle"
        )
    code_tree_hash = next(iter(code_tree_hashes))
    code_bundle_path, code_bundle_sha256 = next(iter(code_bundles))
    code_bundle_binding = LF022ArtifactBinding(
        path=code_bundle_path,
        sha256=code_bundle_sha256,
    )
    source_bundle = _source_file(
        repo_root,
        _safe_relative(code_bundle_path, label="code bundle"),
        label="code bundle",
    )
    if hash_file(source_bundle) != code_bundle_sha256:
        raise LF022HistoricalReplayError("code bundle differs from its admission binding")

    with tempfile.TemporaryDirectory(prefix="leanfaith-lf022-historical-") as temporary:
        private_root = Path(temporary)
        if private_root.stat().st_mode & 0o077:
            raise LF022HistoricalReplayError("historical replay directory is not private")
        historical_root = private_root / "checkout"
        try:
            checkout = materialize_code_bundle_checkout(
                source_bundle,
                historical_root,
                code_tree_hash,
            )
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise LF022HistoricalReplayError(f"historical code bundle rejected: {exc}") from exc
        if checkout.bundle_sha256 != code_bundle_sha256:
            raise LF022HistoricalReplayError("materialized bundle hash differs from admission")
        _copy_binding_closure(
            source_root=repo_root,
            historical_root=historical_root,
            initial=(manifest_binding, code_bundle_binding),
        )
        for loaded in loaded_tasks:
            _copy_task_directory(
                source_root=repo_root,
                historical_root=historical_root,
                executor_output_root=executor_output_root,
                execution_task_id=loaded.task.execution_task_id,
            )
        result = _launch_historical_subprocess(
            historical_root=historical_root,
            current_root=repo_root,
            manifest_binding=manifest_binding,
            code_tree_hash=code_tree_hash,
            code_bundle_sha256=code_bundle_sha256,
            task_count=len(loaded_tasks),
        )
    if (
        result.code_tree_hash != code_tree_hash
        or result.code_bundle_sha256 != code_bundle_sha256
        or len(result.terminal_bindings) != len(loaded_tasks)
    ):
        raise LF022HistoricalReplayError("historical replay result differs from admission")
    expected_task_ids = tuple(sorted(task.task.execution_task_id for task in loaded_tasks))
    observed_task_ids = tuple(item.execution_task_id for item in result.terminal_bindings)
    if observed_task_ids != expected_task_ids:
        raise LF022HistoricalReplayError("historical replay terminal task set drifted")
    return result


__all__ = [
    "LF022HistoricalModuleBinding",
    "LF022HistoricalReplayError",
    "LF022HistoricalReplayResult",
    "LF022HistoricalTerminalBinding",
    "run_lf022_historical_replay",
]
