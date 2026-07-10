"""Git repository probing (LF-010): identity, revision reachability, toolchain.

Probes repository *metadata* only (ls-remote, single-file fetch); cloning and
extraction are Phase 2 work. The ``GitClient`` protocol keeps network I/O
mockable; the real client shells out to ``git ls-remote`` (repository
metadata, not Lean semantics, so outside the §34.6 restriction) and fetches
raw files over HTTPS.
"""

from __future__ import annotations

import datetime
import re
import subprocess
import urllib.request
from typing import Protocol

from leanfaith.config.models import StrictModel
from leanfaith.schemas.enums import AccessStatus, SourceKind
from leanfaith.schemas.source import SourceManifest
from leanfaith.sources.base import ProbeOutcome

_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


class GitClient(Protocol):
    def revision_exists(self, repo_url: str, revision: str) -> bool: ...

    def fetch_file(self, repo_url: str, revision: str, path: str) -> str | None: ...


class RealGitClient:
    """ls-remote + raw.githubusercontent fetch; metadata only, never a clone."""

    def revision_exists(self, repo_url: str, revision: str) -> bool:
        if _SHA_PATTERN.match(revision):
            # ls-remote cannot list arbitrary SHAs; confirm via the raw fetch
            # below instead. Treat a well-formed SHA as "check by content".
            return self.fetch_file(repo_url, revision, "lean-toolchain") is not None
        listed = subprocess.run(
            ["git", "ls-remote", repo_url, revision],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        return listed.returncode == 0 and bool(listed.stdout.strip())

    def fetch_file(self, repo_url: str, revision: str, path: str) -> str | None:
        match = re.match(r"https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", repo_url)
        if match is None:
            return None
        owner, repo = match.groups()
        url = f"https://raw.githubusercontent.com/{owner}/{repo}/{revision}/{path}"
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                body: bytes = response.read()
                return body.decode("utf-8")
        except OSError:
            return None


class GitProbeConfig(StrictModel):
    source: str
    repo_url: str
    revision: str
    root_module: str | None = None
    expected_toolchain: str | None = None


class GitRepositoryProber:
    """Verify a pinned repository revision and record its checked-in toolchain."""

    def __init__(self, config: GitProbeConfig, client: GitClient) -> None:
        self._config = config
        self._client = client
        self.source = config.source

    def probe(self) -> ProbeOutcome:
        config = self._config
        now = datetime.datetime.now(tz=datetime.UTC)
        if not self._client.revision_exists(config.repo_url, config.revision):
            return ProbeOutcome(
                source=config.source,
                blocked=True,
                blocked_reason=(f"revision {config.revision} not reachable at {config.repo_url}"),
                probed_at=now,
            )
        toolchain_raw = self._client.fetch_file(config.repo_url, config.revision, "lean-toolchain")
        toolchain = toolchain_raw.strip() if toolchain_raw else None
        notes = ""
        if (
            config.expected_toolchain is not None
            and toolchain is not None
            and not toolchain.endswith(config.expected_toolchain)
        ):
            notes = (
                f"toolchain mismatch: checked-in {toolchain!r} vs expected "
                f"{config.expected_toolchain!r} (§6.2 decision required before extraction)"
            )
        manifest = SourceManifest(
            source=config.source,
            kind=SourceKind.GIT_REPOSITORY,
            resolved_id=config.repo_url,
            revision=config.revision,
            retrieval_date=now,
            access_status=AccessStatus.PUBLIC,
            project_toolchain=toolchain,
            notes=notes,
        )
        return ProbeOutcome(source=config.source, manifest=manifest, probed_at=now)
