#!/usr/bin/env python3
"""Hash-bound v2 preflight or one-problem EPFL RCP Kimi qualification."""

from __future__ import annotations

import argparse
from pathlib import Path

from leanfaith.config.paths import RepoPaths
from leanfaith.generation import rcp_qualification_v1 as engine
from leanfaith.generation.rcp_qualification_v2 import (
    RCPQualificationV2Error,
    execute_one_rcp_qualification_v2,
    load_rcp_qualification_v2,
    probe_rcp_catalog_v2,
    write_rcp_preflight_v2,
)

_DEFAULT_CONFIG = Path("configs/generation/rcp_kimi_qualification_v2.yaml")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Validate the hash-bound v2 EPFL RCP envelope or execute exactly "
            "one public, reference-blind Kimi qualification. No bulk mode exists."
        )
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--preflight", action="store_true")
    action.add_argument("--execute-one", action="store_true")
    parser.add_argument("--model", choices=("primary", "fallback"), default="primary")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--config", type=Path, default=_DEFAULT_CONFIG)
    args = parser.parse_args()

    paths = RepoPaths.discover(args.root) if args.root is not None else RepoPaths.discover()
    config_path = args.config
    if not config_path.is_absolute():
        config_path = paths.root / config_path
    try:
        loaded = load_rcp_qualification_v2(config_path, repo_root=paths.root)
        credentials = engine.resolve_rcp_credentials(loaded.engine_loaded.loaded_config.config)
        transport = engine.UrllibRCPTransport()
        catalog = probe_rcp_catalog_v2(
            loaded,
            credentials=credentials,
            transport=transport,
        )
        preflight_path, preflight_hash = write_rcp_preflight_v2(
            loaded,
            catalog=catalog,
            repo_root=paths.root,
        )
        if args.preflight:
            print(f"catalog_observation_id={catalog.observation_id}")
            print(f"catalog_observed_at={catalog.observed_at.isoformat()}")
            print(f"catalog_raw_response_sha256={catalog.raw_response_sha256}")
            print(f"preflight_report={preflight_path}")
            print(f"preflight_sha256={preflight_hash}")
            print("provider_requests_created=0")
            print("bulk_collection_performed=false")
            print("semantic_labels_created=false")
            print("gate_credit_claimed=false")
            print("gate_closed=false")
            return 0
        run = execute_one_rcp_qualification_v2(
            loaded,
            catalog=catalog,
            credentials=credentials,
            repo_root=paths.root,
            model_selection=args.model,
            transport=transport,
        )
    except (RCPQualificationV2Error, engine.RCPQualificationError, ValueError, OSError) as exc:
        print(f"FAILED: {exc}")
        return 1

    print(f"catalog_observation_id={catalog.observation_id}")
    print(f"catalog_raw_response_sha256={catalog.raw_response_sha256}")
    print(f"output_directory={run.engine_run.output_directory}")
    print(f"terminal={run.engine_run.terminal_path}")
    print(f"terminal_status={run.engine_run.terminal.status.value}")
    print(f"attempt_count={len(run.engine_run.attempt_paths)}")
    print(f"model={run.engine_run.terminal.model_id}")
    print(f"qualification_manifest={run.manifest_path}")
    print(f"qualification_manifest_id={run.manifest.manifest_id}")
    print("actual_provider_calls_performed=true")
    print("bulk_collection_performed=false")
    print("reference_transmission_performed=false")
    print("private_source_transmission_performed=false")
    print("semantic_labels_created=false")
    print("gate_credit_claimed=false")
    print("gate_closed=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
