#!/usr/bin/env python3
"""Audit training-data readiness without starting training or making labels."""

from __future__ import annotations

import argparse
from pathlib import Path

from leanfaith.config.paths import RepoPaths
from leanfaith.models.data_readiness import (
    audit_training_data_readiness,
    load_training_data_readiness_policy,
    write_training_data_readiness_reports,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Fail-closed audit of prevalence and training-data adequacy. "
            "This command executes no model and creates no semantic labels."
        )
    )
    parser.add_argument("--root", type=Path)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("configs/models/training_data_readiness_v1.yaml"),
    )
    parser.add_argument(
        "--reduced-data-ablation",
        action="store_true",
        help=(
            "Explicitly audit reduced-data mode. This relaxes only the 50k scale "
            "target; label, split, source, cap, and gold-product rules remain binding."
        ),
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Write/print a NOT_READY report but exit zero; default gating mode exits 3.",
    )
    args = parser.parse_args()
    paths = RepoPaths.discover(args.root) if args.root is not None else RepoPaths.discover()
    config_path = args.config if args.config.is_absolute() else paths.root / args.config
    loaded = load_training_data_readiness_policy(config_path)
    report = audit_training_data_readiness(
        repo_root=paths.root,
        loaded_policy=loaded,
        reduced_data_ablation=args.reduced_data_ablation,
    )
    json_path = paths.root / loaded.config.reports.json_path
    markdown_path = paths.root / loaded.config.reports.markdown_path
    write_training_data_readiness_reports(
        report,
        json_path=json_path,
        markdown_path=markdown_path,
    )
    print(f"audit_id={report.audit_id}")
    print(f"status={report.status}")
    print(
        "prevalence_frame_adequate_for_annotation="
        f"{str(report.prevalence.frame_adequate_for_annotation).lower()}"
    )
    print(f"human_terminal_label_count={report.prevalence.human_terminal_label_count}")
    print(f"confirmatory_training_ready={str(report.training.confirmatory_ready).lower()}")
    print(f"json_report={json_path}")
    print(f"markdown_report={markdown_path}")
    print("model_execution_performed=false")
    print("semantic_labels_created=false")
    if report.status == "NOT_READY" and not args.report_only:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
