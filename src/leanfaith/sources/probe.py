"""Hugging Face dataset probing, archival, and fallback resolution (LF-010).

The prober talks to a narrow ``HFClient`` protocol; tests use fakes and the
real client (lazy ``huggingface_hub``/``datasets`` imports) is exercised only
in approved live probe runs. Secrets are resolved at call time by name and
never enter manifests, samples, or hashes (§6.1, §9.2).
"""

from __future__ import annotations

import contextlib
import datetime
import json
from typing import Any, Protocol

from leanfaith.config.hashing import canonical_json_bytes, sha256_hex
from leanfaith.config.models import SecretRef, StrictModel
from leanfaith.config.paths import RepoPaths
from leanfaith.schemas.enums import AccessStatus, NLTrust, SourceKind
from leanfaith.schemas.manifest import write_manifest
from leanfaith.schemas.source import SourceManifest
from leanfaith.sources.base import (
    DatasetProbeInfo,
    ProbeOutcome,
    SourceProbeError,
    SourceProber,
)

PROBE_SAMPLE_ROWS = 100


class HFClient(Protocol):
    """Minimal dataset-hub surface the prober needs."""

    def dataset_info(
        self, dataset_id: str, revision: str | None, token: str | None
    ) -> DatasetProbeInfo: ...

    def load_sample(
        self,
        dataset_id: str,
        revision: str,
        split: str,
        rows: int,
        token: str | None,
    ) -> list[dict[str, Any]]: ...


class AccessBlockedError(RuntimeError):
    """Raised by clients when a source is not accessible (401/403/404/gated)."""


def _hub_token(token: str | None) -> str | bool:
    """huggingface_hub treats ``None`` as "use ambient cached credentials";
    an unauthenticated probe must be genuinely anonymous (a private source
    accidentally succeeding via ambient login would corrupt the §9.2 access
    record), so no-token maps to an explicit ``False``."""
    return token if token is not None else False


class RealHFClient:
    """Live client; lazy imports so unit tests never require the hub stack."""

    def dataset_info(
        self, dataset_id: str, revision: str | None, token: str | None
    ) -> DatasetProbeInfo:
        import datasets as hf_datasets
        from huggingface_hub import HfApi
        from huggingface_hub.errors import (
            GatedRepoError,
            HfHubHTTPError,
            RepositoryNotFoundError,
        )

        api = HfApi(token=_hub_token(token))
        try:
            info = api.dataset_info(dataset_id, revision=revision)
        except (GatedRepoError, RepositoryNotFoundError) as exc:
            raise AccessBlockedError(f"{dataset_id}: {type(exc).__name__}") from exc
        except HfHubHTTPError as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (401, 403, 404):
                raise AccessBlockedError(f"{dataset_id}: HTTP {status}") from exc
            raise
        card: object = info.card_data or {}
        license_name = (
            card.get("license") if isinstance(card, dict) else getattr(card, "license", None)
        )
        splits: dict[str, int | None] = {}
        columns: tuple[str, ...] = ()
        # Schema discovery is best effort; identity/access above is not.
        with contextlib.suppress(Exception):
            split_names = hf_datasets.get_dataset_split_names(dataset_id, token=_hub_token(token))
            splits = dict.fromkeys(split_names)
            builder = hf_datasets.load_dataset_builder(dataset_id, token=_hub_token(token))
            if builder.info.splits:
                splits = {name: split.num_examples for name, split in builder.info.splits.items()}
            if builder.info.features is not None:
                columns = tuple(builder.info.features.keys())
        return DatasetProbeInfo(
            resolved_id=info.id,
            revision=info.sha or (revision or "unknown"),
            license=str(license_name) if license_name is not None else None,
            gated=bool(info.gated),
            splits=splits,
            columns=columns,
        )

    def load_sample(
        self,
        dataset_id: str,
        revision: str,
        split: str,
        rows: int,
        token: str | None,
    ) -> list[dict[str, Any]]:
        import datasets as hf_datasets

        dataset = hf_datasets.load_dataset(
            dataset_id, split=f"{split}[:{rows}]", revision=revision, token=_hub_token(token)
        )
        return [dict(row) for row in dataset]


class HFProbeConfig(StrictModel):
    """One HF-dataset probe target (bound from configs/sources/*.yaml by LF-011)."""

    source: str
    dataset_id: str
    revision: str | None = None
    sample_split: str = "train"
    sample_rows: int = PROBE_SAMPLE_ROWS
    expected_columns: tuple[str, ...] = ()
    nl_trust: NLTrust | None = None
    token: SecretRef | None = None
    external_api_approved: bool | None = None


class HFDatasetProber:
    """Probe an HF dataset: identity, license, schema, archived sample (§9.2)."""

    def __init__(self, config: HFProbeConfig, client: HFClient) -> None:
        self._config = config
        self._client = client
        self.source = config.source

    def probe(self) -> ProbeOutcome:
        config = self._config
        now = datetime.datetime.now(tz=datetime.UTC)
        token = config.token.resolve() if config.token is not None else None
        try:
            info = self._client.dataset_info(config.dataset_id, config.revision, token)
        except AccessBlockedError as exc:
            return ProbeOutcome(
                source=config.source,
                blocked=True,
                blocked_reason=str(exc),
                probed_at=now,
            )

        if config.expected_columns and info.columns:
            missing = set(config.expected_columns) - set(info.columns)
            if missing:
                raise SourceProbeError(
                    f"{config.source}: pinned schema mismatch; missing columns "
                    f"{sorted(missing)} in {info.resolved_id}@{info.revision} (§9.3: "
                    "recheck the mapping at the pinned revision, do not guess)"
                )

        rows = self._client.load_sample(
            config.dataset_id, info.revision, config.sample_split, config.sample_rows, token
        )
        sample_jsonl = (b"\n".join(canonical_json_bytes(row) for row in rows) + b"\n").decode(
            "utf-8"
        )
        sample_hash = sha256_hex(sample_jsonl.encode("utf-8"))

        manifest = SourceManifest(
            source=config.source,
            kind=SourceKind.HF_DATASET,
            resolved_id=info.resolved_id,
            revision=info.revision,
            retrieval_date=now,
            access_status=(
                AccessStatus.PRIVATE_AUTHENTICATED
                if info.gated or config.token is not None
                else AccessStatus.PUBLIC
            ),
            license=info.license,
            columns=info.columns,
            split_counts={k: v for k, v in info.splits.items() if v is not None},
            sample_rows=len(rows),
            sample_hash=sample_hash,
            nl_trust=config.nl_trust,
            external_api_approved=config.external_api_approved,
        )
        return ProbeOutcome(
            source=config.source,
            manifest=manifest,
            sample_jsonl=sample_jsonl,
            sample_hash=sample_hash,
            sample_row_count=len(rows),
            probed_at=now,
        )


def archive_probe(outcome: ProbeOutcome, paths: RepoPaths) -> ProbeOutcome:
    """Archive manifest + raw sample under the declared §7 data paths.

    Blocked outcomes archive a structured blocked manifest (§25 item 10)
    instead of fabricating source data. Returns the outcome with
    ``sample_path`` set and the inline sample payload dropped.
    """
    manifest_path = paths.data / "source_manifests" / f"{outcome.source}.json"
    if outcome.blocked:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        blocked_record = {
            "source": outcome.source,
            "blocked": True,
            "blocked_reason": outcome.blocked_reason,
            "probed_at": outcome.probed_at.isoformat(),
        }
        # A later blocked re-probe must never clobber durable probe evidence:
        # if a successful manifest exists, the block is recorded alongside it.
        target = manifest_path
        if manifest_path.exists():
            try:
                existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                existing = {}
            if not existing.get("blocked"):
                target = manifest_path.with_suffix(".blocked.json")
        target.write_text(json.dumps(blocked_record, sort_keys=True) + "\n", encoding="utf-8")
        return outcome

    if outcome.manifest is None:
        raise SourceProbeError(f"{outcome.source}: outcome has neither manifest nor block")

    sample_path: str | None = None
    if outcome.sample_jsonl is not None:
        payload = outcome.sample_jsonl.encode("utf-8")
        if sha256_hex(payload) != outcome.sample_hash:
            raise SourceProbeError(f"{outcome.source}: sample hash mismatch before archival")
        sample_file = paths.data / "raw" / "sources" / outcome.source / "probe_sample.jsonl"
        sample_file.parent.mkdir(parents=True, exist_ok=True)
        sample_file.write_bytes(payload)
        sample_path = str(sample_file.relative_to(paths.root))

    write_manifest(outcome.manifest, manifest_path)
    return outcome.model_copy(update={"sample_path": sample_path, "sample_jsonl": None})


def run_fallback_chain(
    probers: list[SourceProber],
) -> tuple[ProbeOutcome, list[ProbeOutcome]]:
    """§9.1 fixed fallback order: return the first accessible outcome plus the
    blocked attempts that preceded it. Raises if every source is blocked."""
    blocked: list[ProbeOutcome] = []
    for prober in probers:
        outcome = prober.probe()
        if outcome.accessible:
            return outcome, blocked
        blocked.append(outcome)
    raise SourceProbeError(
        "every source in the fallback order is blocked: "
        + "; ".join(f"{o.source} ({o.blocked_reason})" for o in blocked)
    )
