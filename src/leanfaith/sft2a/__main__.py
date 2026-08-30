"""Command-line entry point for bounded SFT2A preparation and execution."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from leanfaith.sft2a.activation import (
    activation_summary,
    load_pilot_activation,
    materialize_authorized_activation,
    preview_authorized_activation,
)
from leanfaith.sft2a.config import load_sft2a_config
from leanfaith.sft2a.detached import (
    launch_detached_pilot,
    pilot_health,
    preflight_detached_launch,
    run_detached_worker,
)
from leanfaith.sft2a.layout import run_paths
from leanfaith.sft2a.legacy import adapt_legacy
from leanfaith.sft2a.legacy_rejudge import (
    prepare_legacy_opus_sample,
    run_legacy_opus_rejudge,
)
from leanfaith.sft2a.pilot import PilotError, prepare_pilot_sample, verify_pilot_replay
from leanfaith.sft2a.pilot_audit import run_pilot_lemex_audit
from leanfaith.sft2a.pipeline import run_lemex_audit, run_one_root, verify_one_root_replay
from leanfaith.sft2a.readiness import load_pilot_readiness
from leanfaith.sft2a.release import compare_fable_and_opus, materialize_post_audit_core


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--pilot-config", type=Path)
    parser.add_argument("--activation-plan", type=Path)
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("verify-config")
    subcommands.add_parser("verify-pilot-readiness")
    subcommands.add_parser("adapt-legacy")
    subcommands.add_parser("run-one-root")
    subcommands.add_parser("verify-replay")
    subcommands.add_parser("run-lemex-audit")
    subcommands.add_parser("compare-fable-opus")
    subcommands.add_parser("prepare-diverse-pilot")
    subcommands.add_parser("run-diverse-pilot")
    subcommands.add_parser("verify-pilot-replay")
    subcommands.add_parser("run-pilot-lemex-audit")
    subcommands.add_parser("launch-authorized-pilot")
    subcommands.add_parser("resume-authorized-pilot")
    subcommands.add_parser("pilot-health")
    subcommands.add_parser("preflight-authorized-pilot")
    subcommands.add_parser("detached-pilot-worker")
    subcommands.add_parser("preview-pilot-activation")
    activation_parser = subcommands.add_parser("activate-authorized-pilot")
    activation_parser.add_argument("--authorization-sentence", required=True)
    subcommands.add_parser("prepare-legacy-opus-sample")
    subcommands.add_parser("run-legacy-opus-rejudge")
    subcommands.add_parser("materialize-fable-post-audit-core")
    arguments = parser.parse_args()
    loaded = load_sft2a_config(
        arguments.config,
        verify_binaries=arguments.command
        in {
            "run-one-root",
            "run-lemex-audit",
            "detached-pilot-worker",
        },
    )
    readiness_commands = {
        "verify-pilot-readiness",
        "prepare-diverse-pilot",
        "run-diverse-pilot",
        "verify-pilot-replay",
        "run-pilot-lemex-audit",
        "launch-authorized-pilot",
        "resume-authorized-pilot",
        "pilot-health",
        "preflight-authorized-pilot",
        "detached-pilot-worker",
        "prepare-legacy-opus-sample",
        "run-legacy-opus-rejudge",
    }
    readiness = (
        load_pilot_readiness(loaded, arguments.pilot_config)
        if arguments.command in readiness_commands
        else None
    )
    if arguments.command == "verify-config":
        result: object = {
            "config_hash": loaded.config_hash,
            "repr_freeze_commit": loaded.config.repr.freeze_commit,
            "status": loaded.config.status,
        }
    elif arguments.command == "verify-pilot-readiness":
        assert readiness is not None
        result = {
            "config_id": readiness.config.config_id,
            "config_hash": readiness.config_hash,
            "status": readiness.config.status,
            "sample_sha256": readiness.config.expected_sample_sha256,
            "pilot_authorized": readiness.authorization.get("authorized") is True,
            "legacy_rejudge_authorized": readiness.config.legacy_rejudge.authorized,
            "historical_fable_combined_tree_sha256": readiness.historical_seal.get(
                "combined_tree_sha256"
            ),
        }
    elif arguments.command == "adapt-legacy":
        adapted = adapt_legacy(loaded)
        result = {
            "output_root": str(adapted.output_root),
            "replayed": adapted.replayed,
            **adapted.manifest,
        }
    elif arguments.command == "run-one-root":
        run = run_one_root(loaded)
        result = {"output_root": str(run.output_root), "replayed": run.replayed, **run.manifest}
    elif arguments.command == "verify-replay":
        result = verify_one_root_replay(loaded)
    elif arguments.command == "run-lemex-audit":
        audit = run_lemex_audit(loaded)
        result = {
            "output_root": str(audit.output_root),
            "replayed": audit.replayed,
            **audit.manifest,
        }
    elif arguments.command == "compare-fable-opus":
        comparison = compare_fable_and_opus(loaded)
        result = {
            "output_root": str(comparison.output_root),
            "replayed": comparison.replayed,
            **comparison.manifest,
        }
    elif arguments.command == "prepare-diverse-pilot":
        assert readiness is not None
        result = prepare_pilot_sample(loaded, readiness)
    elif arguments.command == "run-diverse-pilot":
        raise PilotError(
            "the production pilot may run only through launch-authorized-pilot under tmux"
        )
    elif arguments.command == "verify-pilot-replay":
        assert readiness is not None
        result = verify_pilot_replay(loaded, readiness)
    elif arguments.command == "run-pilot-lemex-audit":
        assert readiness is not None
        result = run_pilot_lemex_audit(loaded, readiness)
    elif arguments.command == "launch-authorized-pilot":
        assert readiness is not None
        result = launch_detached_pilot(loaded, readiness, resume=False)
    elif arguments.command == "resume-authorized-pilot":
        assert readiness is not None
        result = launch_detached_pilot(loaded, readiness, resume=True)
    elif arguments.command == "pilot-health":
        assert readiness is not None
        result = pilot_health(loaded, readiness)
    elif arguments.command == "preflight-authorized-pilot":
        assert readiness is not None
        result = preflight_detached_launch(loaded, readiness, resume=False)
    elif arguments.command == "detached-pilot-worker":
        assert readiness is not None
        result = run_detached_worker(loaded, readiness)
    elif arguments.command == "preview-pilot-activation":
        activation = load_pilot_activation(loaded, arguments.activation_plan)
        preview = preview_authorized_activation(activation)
        result = activation_summary(activation, preview)
    elif arguments.command == "activate-authorized-pilot":
        activation = load_pilot_activation(loaded, arguments.activation_plan)
        activated = materialize_authorized_activation(
            activation,
            authorization_sentence=arguments.authorization_sentence,
        )
        result = {
            **activation_summary(activation, activated),
            "authorized_artifacts_materialized": True,
            "launch_started": False,
        }
    elif arguments.command == "prepare-legacy-opus-sample":
        assert readiness is not None
        result = prepare_legacy_opus_sample(loaded, readiness)
    elif arguments.command == "run-legacy-opus-rejudge":
        assert readiness is not None
        result = run_legacy_opus_rejudge(loaded, readiness)
    else:
        paths = run_paths(loaded)
        release = materialize_post_audit_core(
            source_run=paths.historical_fable_one_root,
            audit_run=paths.historical_fable_audit,
            output_root=paths.post_audit,
        )
        result = {
            "output_root": str(release.output_root),
            "replayed": release.replayed,
            **release.manifest,
        }
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
