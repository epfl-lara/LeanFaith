"""Deterministic source bundles for dirty-worktree gate-closing runs."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from leanfaith.config.hashing import hash_file
from leanfaith.config.models import StrictModel
from leanfaith.schemas.manifest import CodeState, collect_code_state

_MANIFEST_NAME = "CODE_BUNDLE_MANIFEST.json"
_HEX64 = r"^[0-9a-f]{64}$"


class _CodeBundleFile(StrictModel):
    """One regular file declared by the bundle manifest."""

    path: str = Field(min_length=1, strict=True)
    sha256: str = Field(pattern=_HEX64, strict=True)
    mode: int = Field(ge=0, le=0o777, strict=True)


class _CodeBundleManifest(StrictModel):
    """Closed schema for the manifest embedded in a code bundle."""

    schema_version: Literal[1]
    code_state: CodeState
    files: tuple[_CodeBundleFile, ...]


@dataclass(frozen=True, slots=True)
class MaterializedCodeCheckout:
    """A clean Git checkout reconstructed from one validated code bundle."""

    root: Path
    bundle_sha256: str
    code_tree_hash: str
    git_revision: str
    file_count: int


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r} in code bundle manifest")
        result[key] = value
    return result


def _reject_nonfinite_json(text: str) -> float:
    raise ValueError(f"non-finite JSON constant {text!r} in code bundle manifest")


def _validate_relative_path(value: str, *, field: str) -> None:
    """Require one canonical POSIX path strictly below the bundle root."""

    if "\\" in value:
        raise ValueError(f"{field} contains a backslash: {value!r}")
    if value.startswith("/"):
        raise ValueError(f"{field} must be relative: {value!r}")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"{field} is not a safe canonical relative path: {value!r}")
    if value == _MANIFEST_NAME:
        raise ValueError(f"{field} collides with the code bundle manifest")


def _read_member_bytes(archive: tarfile.TarFile, member: tarfile.TarInfo) -> bytes:
    handle = archive.extractfile(member)
    if handle is None:
        raise ValueError(f"code bundle member {member.name!r} is unreadable")
    return handle.read()


def _listed_files(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard", "-z"],
        cwd=root,
        capture_output=True,
        check=True,
    )
    return sorted(path.decode("utf-8") for path in result.stdout.split(b"\0") if path)


def freeze_code_bundle(root: Path, output_dir: Path) -> tuple[Path, str, CodeState]:
    """Archive every file contributing to ``code_tree_hash`` with stable bytes."""

    state = collect_code_state(root)
    files = _listed_files(root)
    entries: list[dict[str, Any]] = []
    for relative in files:
        _validate_relative_path(relative, field="code bundle input path")
        path = root / relative
        if path.is_symlink():
            raise ValueError(f"code bundle refuses symlink input: {relative}")
        if not path.is_file():
            continue
        entries.append(
            {
                "path": relative,
                "sha256": hash_file(path),
                "mode": path.stat().st_mode & 0o777,
            }
        )
    if not entries:
        raise ValueError("code bundle requires at least one regular file")
    manifest = {
        "schema_version": 1,
        "code_state": state.model_dump(mode="json"),
        "files": entries,
    }
    manifest_bytes = (
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")

    output_dir.mkdir(parents=True, exist_ok=True)
    temporary = output_dir / ".code_bundle.tar.gz.partial"
    with (
        temporary.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w") as archive,
    ):
        manifest_info = tarfile.TarInfo(_MANIFEST_NAME)
        manifest_info.size = len(manifest_bytes)
        manifest_info.mtime = 0
        manifest_info.mode = 0o644
        archive.addfile(manifest_info, io.BytesIO(manifest_bytes))
        for entry in entries:
            payload = (root / entry["path"]).read_bytes()
            info = tarfile.TarInfo(str(entry["path"]))
            info.size = len(payload)
            info.mtime = 0
            info.mode = int(entry["mode"])
            archive.addfile(info, io.BytesIO(payload))
    digest = hash_file(temporary)
    final = output_dir / f"code_bundle_{digest}.tar.gz"
    os.replace(temporary, final)
    return final, digest, state


def validate_code_bundle(path: Path, expected_code_tree_hash: str) -> str:
    """Verify a frozen code bundle without extracting it.

    Validation is fail-closed: the embedded manifest has a closed schema, all
    member paths are canonical and relative, the archive contains exactly one
    regular member for every declaration (and no others), and bytes plus modes
    match the manifest.
    """

    if not path.is_file():
        raise FileNotFoundError(path)
    with tarfile.open(path, mode="r:gz") as archive:
        members = archive.getmembers()
        names: set[str] = set()
        for member in members:
            if member.name in names:
                raise ValueError(f"duplicate code bundle archive member: {member.name!r}")
            names.add(member.name)
            if member.name != _MANIFEST_NAME:
                _validate_relative_path(member.name, field="archive member path")

        manifest_members = [member for member in members if member.name == _MANIFEST_NAME]
        if len(manifest_members) != 1:
            raise ValueError(
                "code bundle must contain exactly one "
                f"{_MANIFEST_NAME}; found {len(manifest_members)}"
            )
        manifest_member = manifest_members[0]
        if not manifest_member.isfile():
            raise ValueError("code bundle manifest must be a regular file")
        if manifest_member.mode & 0o777 != 0o644:
            raise ValueError("code bundle manifest mode must be 0644")

        try:
            manifest_data = json.loads(
                _read_member_bytes(archive, manifest_member).decode("utf-8"),
                object_pairs_hook=_reject_duplicate_json_keys,
                parse_constant=_reject_nonfinite_json,
            )
            manifest = _CodeBundleManifest.model_validate(manifest_data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid code bundle manifest JSON: {exc}") from exc

        if not manifest.files:
            raise ValueError("code bundle manifest must declare at least one file")

        declared: dict[str, _CodeBundleFile] = {}
        for entry in manifest.files:
            _validate_relative_path(entry.path, field="manifest file path")
            if entry.path in declared:
                raise ValueError(f"duplicate code bundle manifest path: {entry.path!r}")
            declared[entry.path] = entry

        expected_names = {_MANIFEST_NAME, *declared}
        missing = sorted(expected_names - names)
        extras = sorted(names - expected_names)
        if missing:
            raise ValueError(f"code bundle is missing declared members: {missing!r}")
        if extras:
            raise ValueError(f"code bundle contains undeclared members: {extras!r}")

        by_name = {member.name: member for member in members}
        for relative, entry in declared.items():
            member = by_name[relative]
            if not member.isfile():
                raise ValueError(f"code bundle member {relative!r} is not a regular file")
            observed_mode = member.mode & 0o777
            if observed_mode != entry.mode:
                raise ValueError(
                    f"code bundle member {relative!r} mode {observed_mode:#05o} "
                    f"does not match declared mode {entry.mode:#05o}"
                )
            digest = hashlib.sha256(_read_member_bytes(archive, member)).hexdigest()
            if digest != entry.sha256:
                raise ValueError(
                    f"code bundle member {relative!r} sha256 {digest} "
                    f"does not match declared sha256 {entry.sha256}"
                )

    observed = manifest.code_state.code_tree_hash
    if observed != expected_code_tree_hash:
        raise ValueError(
            f"code bundle tree hash {observed!r} does not match "
            f"run tree {expected_code_tree_hash!r}"
        )
    return hash_file(path)


def _manifest_from_archive(archive: tarfile.TarFile) -> _CodeBundleManifest:
    """Parse the already-validated manifest for materialization."""

    members = archive.getmembers()
    manifest_members = [member for member in members if member.name == _MANIFEST_NAME]
    if len(manifest_members) != 1:
        raise ValueError(
            f"code bundle must contain exactly one {_MANIFEST_NAME}; found {len(manifest_members)}"
        )
    try:
        manifest_data = json.loads(
            _read_member_bytes(archive, manifest_members[0]).decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_nonfinite_json,
        )
        return _CodeBundleManifest.model_validate(manifest_data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid code bundle manifest JSON: {exc}") from exc


def _initialize_clean_checkout(root: Path, *, bundle_sha256: str) -> CodeState:
    """Commit materialized files with fixed identity and timestamps."""

    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "LeanFaith Code Bundle"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "code-bundle@leanfaith.invalid"],
        cwd=root,
        check=True,
    )
    subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=root, check=True)
    subprocess.run(["git", "config", "core.filemode", "true"], cwd=root, check=True)
    subprocess.run(["git", "add", "--force", "--all"], cwd=root, check=True)
    commit_environment = {
        **os.environ,
        "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00",
        "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
    }
    subprocess.run(
        [
            "git",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-q",
            "-m",
            f"Materialize LeanFaith code bundle {bundle_sha256}",
        ],
        cwd=root,
        check=True,
        env=commit_environment,
    )
    return collect_code_state(root)


def materialize_code_bundle_checkout(
    path: Path,
    destination: Path,
    expected_code_tree_hash: str,
) -> MaterializedCodeCheckout:
    """Reconstruct a validated bundle as an exact, clean Git checkout.

    The destination must not exist. Files are written into a sibling temporary
    directory, checked byte-for-byte against the bundle manifest, committed
    with fixed Git metadata, and atomically renamed only after the clean
    checkout reproduces ``expected_code_tree_hash``. The embedded manifest is
    deliberately not added to the checkout because it was not part of the
    source tree used to compute that hash.
    """

    bundle_sha256 = validate_code_bundle(path, expected_code_tree_hash)
    destination = destination.resolve()
    if destination.exists():
        raise FileExistsError(f"code bundle destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.partial-",
            dir=destination.parent,
        )
    )
    try:
        with tarfile.open(path, mode="r:gz") as archive:
            manifest = _manifest_from_archive(archive)
            by_name = {member.name: member for member in archive.getmembers()}
            for entry in manifest.files:
                member = by_name[entry.path]
                payload = _read_member_bytes(archive, member)
                observed_hash = hashlib.sha256(payload).hexdigest()
                if observed_hash != entry.sha256:
                    raise ValueError(
                        f"code bundle member {entry.path!r} sha256 {observed_hash} "
                        f"does not match declared sha256 {entry.sha256}"
                    )
                target = temporary / entry.path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
                target.chmod(entry.mode)

        if hash_file(path) != bundle_sha256:
            raise ValueError("code bundle changed while it was being materialized")
        state = _initialize_clean_checkout(temporary, bundle_sha256=bundle_sha256)
        if state.git_dirty:
            raise ValueError("materialized code bundle checkout is unexpectedly dirty")
        if state.code_tree_hash != expected_code_tree_hash:
            raise ValueError(
                f"materialized checkout tree hash {state.code_tree_hash!r} does not match "
                f"expected tree {expected_code_tree_hash!r}"
            )
        os.replace(temporary, destination)
        return MaterializedCodeCheckout(
            root=destination,
            bundle_sha256=bundle_sha256,
            code_tree_hash=expected_code_tree_hash,
            git_revision=state.git_revision,
            file_count=len(manifest.files),
        )
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
