"""LF-010: source probe framework with fake clients (§9.1, §9.2, §9.5)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from leanfaith.config.models import SecretRef
from leanfaith.config.paths import RepoPaths
from leanfaith.schemas.enums import AccessStatus, NLTrust
from leanfaith.schemas.manifest import read_manifest
from leanfaith.schemas.source import SourceManifest
from leanfaith.sources import (
    DatasetProbeInfo,
    GitProbeConfig,
    GitRepositoryProber,
    HFDatasetProber,
    HFProbeConfig,
    SourceProbeError,
    archive_probe,
    run_fallback_chain,
)
from leanfaith.sources.probe import AccessBlockedError

_ROWS = [
    {"uuid": "a", "question": "Prove 1+1=2.", "answer": "trivial", "lean_code": "theorem ..."},
    {"uuid": "b", "question": "Prove 2+2=4.", "answer": "trivial", "lean_code": "theorem ..."},
]


class FakeHFClient:
    def __init__(
        self,
        *,
        blocked_without_token: bool = False,
        columns: tuple[str, ...] = ("uuid", "question", "answer", "lean_code"),
        gated: bool = False,
    ) -> None:
        self.blocked_without_token = blocked_without_token
        self.columns = columns
        self.gated = gated
        self.seen_tokens: list[str | None] = []

    def dataset_info(
        self, dataset_id: str, revision: str | None, token: str | None
    ) -> DatasetProbeInfo:
        self.seen_tokens.append(token)
        if self.blocked_without_token and token is None:
            raise AccessBlockedError(f"{dataset_id}: HTTP 401")
        return DatasetProbeInfo(
            resolved_id=dataset_id,
            revision="abc123",
            license="mit",
            gated=self.gated,
            splits={"train": 99774},
            columns=self.columns,
        )

    def load_sample(
        self, dataset_id: str, revision: str, split: str, rows: int, token: str | None
    ) -> list[dict[str, Any]]:
        return _ROWS[:rows]


def _config(**overrides: Any) -> HFProbeConfig:
    payload: dict[str, Any] = {
        "source": "sft_classic_numina",
        "dataset_id": "formalmathatepfl/sft_classic_numina",
        "expected_columns": ("uuid", "question", "answer", "lean_code"),
        "nl_trust": NLTrust.TRUSTED,
    }
    payload.update(overrides)
    return HFProbeConfig.model_validate(payload)


def _paths(tmp_path: Path) -> RepoPaths:
    (tmp_path / "PLAN.md").write_text("plan\n")
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    return RepoPaths(root=tmp_path)


def test_successful_probe_builds_manifest() -> None:
    outcome = HFDatasetProber(_config(), FakeHFClient()).probe()
    assert outcome.accessible
    manifest = outcome.manifest
    assert manifest is not None
    assert manifest.resolved_id == "formalmathatepfl/sft_classic_numina"
    assert manifest.revision == "abc123"
    assert manifest.license == "mit"
    assert manifest.split_counts == {"train": 99774}
    assert manifest.sample_rows == 2
    assert manifest.access_status is AccessStatus.PUBLIC
    assert outcome.sample_hash is not None and len(outcome.sample_hash) == 64


def test_sample_hash_is_deterministic() -> None:
    first = HFDatasetProber(_config(), FakeHFClient()).probe()
    second = HFDatasetProber(_config(), FakeHFClient()).probe()
    assert first.sample_hash == second.sample_hash


def test_schema_mismatch_fails_closed() -> None:
    client = FakeHFClient(columns=("uuid", "question"))
    with pytest.raises(SourceProbeError, match="missing columns"):
        HFDatasetProber(_config(), client).probe()


def test_blocked_source_returns_structured_block() -> None:
    client = FakeHFClient(blocked_without_token=True)
    outcome = HFDatasetProber(_config(), client).probe()
    assert outcome.blocked and not outcome.accessible
    assert outcome.blocked_reason is not None and "401" in outcome.blocked_reason


def test_token_resolved_by_name_and_never_stored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEANFAITH_TEST_HF_TOKEN", "hf_secret_value")
    client = FakeHFClient(blocked_without_token=True, gated=True)
    config = _config(
        token=SecretRef(env="LEANFAITH_TEST_HF_TOKEN"),
        access_basis="test fixture authorization",
        institutional_policy_status="approved_for_test",
        license_status="undeclared",
        redistribution_allowed=False,
        external_transmission_allowed=False,
        release_eligibility=False,
        external_api_approved=False,
    )
    outcome = HFDatasetProber(config, client).probe()
    assert outcome.accessible
    assert client.seen_tokens == ["hf_secret_value"]
    assert outcome.manifest is not None
    assert outcome.manifest.access_status is AccessStatus.PRIVATE_AUTHENTICATED
    dumped = outcome.model_dump_json()
    assert "hf_secret_value" not in dumped


def test_archive_probe_writes_manifest_and_sample(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    outcome = HFDatasetProber(_config(), FakeHFClient()).probe()
    archived = archive_probe(outcome, paths)
    manifest_file = paths.data / "source_manifests" / "sft_classic_numina.json"
    assert read_manifest(manifest_file, SourceManifest) == outcome.manifest
    assert archived.sample_path == "data/raw/sources/sft_classic_numina/probe_sample.jsonl"
    assert archived.sample_jsonl is None
    sample_file = paths.root / archived.sample_path
    lines = sample_file.read_text().strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0])["uuid"] == "a"


def test_archive_probe_writes_blocked_record(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    outcome = HFDatasetProber(_config(), FakeHFClient(blocked_without_token=True)).probe()
    archive_probe(outcome, paths)
    record = json.loads((paths.data / "source_manifests" / "sft_classic_numina.json").read_text())
    assert record["blocked"] is True
    assert "401" in record["blocked_reason"]


def test_fallback_chain_picks_first_accessible() -> None:
    blocked = HFDatasetProber(
        _config(source="sft_classic", dataset_id="formalmathatepfl/sft_classic"),
        FakeHFClient(blocked_without_token=True),
    )
    accessible = HFDatasetProber(_config(), FakeHFClient())
    outcome, attempts = run_fallback_chain([blocked, accessible])
    assert outcome.source == "sft_classic_numina"
    assert [a.source for a in attempts] == ["sft_classic"]
    assert attempts[0].blocked


def test_fallback_chain_exhausted_raises() -> None:
    probers = [
        HFDatasetProber(
            _config(source=name, dataset_id=name),
            FakeHFClient(blocked_without_token=True),
        )
        for name in ("one", "two")
    ]
    with pytest.raises(SourceProbeError, match="every source"):
        run_fallback_chain(probers)


# --- git repository probing ---


class FakeGitClient:
    def __init__(self, *, exists: bool = True, toolchain: str | None) -> None:
        self.exists = exists
        self.toolchain = toolchain

    def revision_exists(self, repo_url: str, revision: str) -> bool:
        return self.exists

    def fetch_file(self, repo_url: str, revision: str, path: str) -> str | None:
        return self.toolchain


def _git_config(**overrides: Any) -> GitProbeConfig:
    payload: dict[str, Any] = {
        "source": "mathlib",
        "repo_url": "https://github.com/leanprover-community/mathlib4",
        "revision": "d568c8c09630de097a046763c17b9ea99f95f950",
        "root_module": "Mathlib",
        "expected_toolchain": "v4.31.0-rc1",
    }
    payload.update(overrides)
    return GitProbeConfig.model_validate(payload)


def test_git_probe_records_toolchain() -> None:
    client = FakeGitClient(toolchain="leanprover/lean4:v4.31.0-rc1\n")
    outcome = GitRepositoryProber(_git_config(), client).probe()
    assert outcome.accessible
    assert outcome.manifest is not None
    assert outcome.manifest.project_toolchain == "leanprover/lean4:v4.31.0-rc1"
    assert outcome.manifest.notes == ""


def test_git_probe_flags_toolchain_mismatch() -> None:
    client = FakeGitClient(toolchain="leanprover/lean4:v4.32.0-rc1\n")
    outcome = GitRepositoryProber(_git_config(source="cslib"), client).probe()
    assert outcome.manifest is not None
    assert "mismatch" in outcome.manifest.notes


def test_git_probe_unreachable_revision_blocks() -> None:
    client = FakeGitClient(exists=False, toolchain=None)
    outcome = GitRepositoryProber(_git_config(), client).probe()
    assert outcome.blocked
    assert outcome.blocked_reason is not None
    assert "not reachable" in outcome.blocked_reason
