#!/usr/bin/env python3
"""Preflight or execute one reference-blind EPFL RCP Kimi qualification."""

from __future__ import annotations

import argparse
from pathlib import Path

from leanfaith.config.paths import RepoPaths
from leanfaith.generation.rcp_qualification_v1 import (
    RCPQualificationError,
    UrllibRCPTransport,
    execute_one_rcp_qualification,
    load_rcp_qualification,
    probe_rcp_catalog,
    resolve_rcp_credentials,
    write_rcp_preflight,
)

_DEFAULT_CONFIG = Path("configs/generation/rcp_kimi_qualification_v1.yaml")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Validate the live EPFL RCP catalog or explicitly execute exactly "
            "one public, reference-blind Kimi qualification. No bulk mode exists."
        )
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument(
        "--preflight",
        action="store_true",
        help="Probe /models and persist only a zero-call qualification preflight.",
    )
    action.add_argument(
        "--execute-one",
        action="store_true",
        help="Execute exactly one public qualification after the live catalog check.",
    )
    parser.add_argument(
        "--model",
        choices=("primary", "fallback"),
        default="primary",
        help="Use K2.7-Code primary or K2.6 fallback/ablation.",
    )
    parser.add_argument("--root", type=Path)
    parser.add_argument("--config", type=Path, default=_DEFAULT_CONFIG)
    args = parser.parse_args()

    paths = RepoPaths.discover(args.root) if args.root is not None else RepoPaths.discover()
    config_path = args.config
    if not config_path.is_absolute():
        config_path = paths.root / config_path
    try:
        loaded = load_rcp_qualification(config_path, repo_root=paths.root)
        credentials = resolve_rcp_credentials(loaded.loaded_config.config)
        transport = UrllibRCPTransport()
        catalog = probe_rcp_catalog(
            loaded,
            credentials=credentials,
            transport=transport,
        )
        preflight_path, preflight_hash = write_rcp_preflight(
            loaded,
            catalog=catalog,
            repo_root=paths.root,
        )
        if args.preflight:
            print(f"catalog_observation_id={catalog.observation_id}")
            print(f"catalog_observed_at={catalog.observed_at.isoformat()}")
            print(f"catalog_raw_response_sha256={catalog.raw_response_sha256}")
            print(f"primary_model={loaded.loaded_config.config.models.primary.model_id}")
            print(f"fallback_model={loaded.loaded_config.config.models.fallback.model_id}")
            print(f"preflight_report={preflight_path}")
            print(f"preflight_sha256={preflight_hash}")
            print("provider_requests_created=0")
            print("reference_transmission_performed=false")
            print("private_source_transmission_performed=false")
            print("semantic_labels_created=false")
            print("gate_credit_claimed=false")
            print("gate_closed=false")
            return 0

        run = execute_one_rcp_qualification(
            loaded,
            catalog=catalog,
            credentials=credentials,
            repo_root=paths.root,
            model_selection=args.model,
            transport=transport,
        )
    except (RCPQualificationError, ValueError, OSError) as exc:
        print(f"FAILED: {exc}")
        return 1

    print(f"catalog_observation_id={catalog.observation_id}")
    print(f"catalog_raw_response_sha256={catalog.raw_response_sha256}")
    print(f"output_directory={run.output_directory}")
    print(f"terminal={run.terminal_path}")
    print(f"terminal_status={run.terminal.status.value}")
    print(f"attempt_count={len(run.attempt_paths)}")
    print(f"model={run.terminal.model_id}")
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
