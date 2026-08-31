"""Command-line entry point for bounded SFT2A preparation and execution."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from leanfaith.config.hashing import hash_file
from leanfaith.sft2a.activation import (
    activation_summary,
    load_pilot_activation,
    materialize_authorized_activation,
    preview_authorized_activation,
)
from leanfaith.sft2a.canaries import run_closure_canaries
from leanfaith.sft2a.census import prepare_rehearsal_sample, run_zero_lean_census
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
from leanfaith.sft2a.parallel_rehearsal import prepare_parallel_rehearsal_path
from leanfaith.sft2a.pilot import PilotError, prepare_pilot_sample, verify_pilot_replay
from leanfaith.sft2a.pilot_audit import run_pilot_lemex_audit
from leanfaith.sft2a.pipeline import run_lemex_audit, run_one_root, verify_one_root_replay
from leanfaith.sft2a.provider_rehearsal_v52 import (
    authorization_sentence_v52,
    launch_provider_rehearsal_v52,
    load_provider_authorization_v52,
    load_provider_rehearsal_v52,
    materialize_provider_authorization_v52,
    preflight_provider_launch_v52,
    prepare_provider_readiness_v52,
    provider_rehearsal_health_v52,
    run_detached_provider_rehearsal_v52,
)
from leanfaith.sft2a.readiness import load_pilot_readiness
from leanfaith.sft2a.reference_certification import (
    launch_detached_reference_certification,
    load_reference_authorization,
    materialize_reference_authorization,
    preflight_reference_launch,
    prepare_reference_pool,
    reference_certification_health,
    run_detached_reference_certification_worker,
    run_reference_certification,
    verify_global_reference_preflight,
    verify_reference_replay,
)
from leanfaith.sft2a.rehearsal import (
    launch_detached_rehearsal,
    load_rehearsal_authorization,
    preflight_rehearsal_launch,
    run_detached_rehearsal_worker,
    run_rehearsal_audit,
    verify_rehearsal_replay,
)
from leanfaith.sft2a.release import compare_fable_and_opus, materialize_post_audit_core


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--pilot-config", type=Path)
    parser.add_argument("--activation-plan", type=Path)
    parser.add_argument("--rehearsal-authorization", type=Path)
    parser.add_argument("--reference-certification-authorization", type=Path)
    parser.add_argument("--provider-rehearsal-config", type=Path)
    parser.add_argument("--provider-rehearsal-authorization", type=Path)
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
    subcommands.add_parser("run-closure-canaries")
    subcommands.add_parser("run-v5-census")
    subcommands.add_parser("prepare-v5-rehearsal")
    subcommands.add_parser("verify-v5-rehearsal-replay")
    subcommands.add_parser("run-v5-rehearsal-audit")
    subcommands.add_parser("preflight-v5-rehearsal")
    subcommands.add_parser("launch-v5-rehearsal")
    subcommands.add_parser("detached-v5-rehearsal-worker")
    subcommands.add_parser("prepare-reference-pool")
    subcommands.add_parser("materialize-reference-certification-authorization")
    subcommands.add_parser("run-reference-certification")
    subcommands.add_parser("verify-reference-certification-replay")
    subcommands.add_parser("verify-global-reference-preflight")
    subcommands.add_parser("preflight-reference-certification")
    subcommands.add_parser("launch-reference-certification")
    subcommands.add_parser("reference-certification-health")
    subcommands.add_parser("detached-reference-certification-worker")
    subcommands.add_parser("prepare-parallel-rehearsal-path")
    subcommands.add_parser("prepare-provider-readiness-v5-2")
    subcommands.add_parser("preview-provider-authorization-v5-2")
    provider_activation = subcommands.add_parser("materialize-provider-authorization-v5-2")
    provider_activation.add_argument("--authorization-sentence", required=True)
    subcommands.add_parser("preflight-provider-rehearsal-v5-2")
    subcommands.add_parser("launch-provider-rehearsal-v5-2")
    subcommands.add_parser("resume-provider-rehearsal-v5-2")
    subcommands.add_parser("provider-rehearsal-v5-2-health")
    subcommands.add_parser("detached-provider-rehearsal-v5-2-worker")
    arguments = parser.parse_args()
    loaded = load_sft2a_config(
        arguments.config,
        verify_binaries=arguments.command
        in {
            "run-one-root",
            "run-lemex-audit",
            "detached-pilot-worker",
            "run-closure-canaries",
            "run-v5-rehearsal-audit",
            "detached-v5-rehearsal-worker",
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
    rehearsal_commands = {
        "verify-v5-rehearsal-replay",
        "run-v5-rehearsal-audit",
        "preflight-v5-rehearsal",
        "launch-v5-rehearsal",
        "detached-v5-rehearsal-worker",
    }
    rehearsal_authorization = (
        load_rehearsal_authorization(loaded, arguments.rehearsal_authorization)
        if arguments.command in rehearsal_commands and arguments.rehearsal_authorization is not None
        else None
    )
    if arguments.command in rehearsal_commands and rehearsal_authorization is None:
        parser.error("--rehearsal-authorization is required for this v5 command")
    reference_commands = {
        "run-reference-certification",
        "preflight-reference-certification",
        "launch-reference-certification",
        "detached-reference-certification-worker",
    }
    reference_authorization = (
        load_reference_authorization(loaded, arguments.reference_certification_authorization)
        if arguments.command in reference_commands
        and arguments.reference_certification_authorization is not None
        else None
    )
    if arguments.command in reference_commands and reference_authorization is None:
        parser.error("--reference-certification-authorization is required for this v5.2 command")
    provider_commands = {
        "prepare-provider-readiness-v5-2",
        "preview-provider-authorization-v5-2",
        "materialize-provider-authorization-v5-2",
        "preflight-provider-rehearsal-v5-2",
        "launch-provider-rehearsal-v5-2",
        "resume-provider-rehearsal-v5-2",
        "provider-rehearsal-v5-2-health",
        "detached-provider-rehearsal-v5-2-worker",
    }
    provider_loaded = (
        load_provider_rehearsal_v52(arguments.provider_rehearsal_config)
        if arguments.command in provider_commands
        and arguments.provider_rehearsal_config is not None
        else None
    )
    if arguments.command in provider_commands and provider_loaded is None:
        parser.error("--provider-rehearsal-config is required for this v5.2 command")
    provider_authorized_commands = {
        "launch-provider-rehearsal-v5-2",
        "resume-provider-rehearsal-v5-2",
        "detached-provider-rehearsal-v5-2-worker",
    }
    provider_authorization = (
        load_provider_authorization_v52(provider_loaded, arguments.provider_rehearsal_authorization)
        if provider_loaded is not None
        and arguments.command in provider_authorized_commands
        and arguments.provider_rehearsal_authorization is not None
        else None
    )
    if arguments.command in provider_authorized_commands and provider_authorization is None:
        parser.error("--provider-rehearsal-authorization is required for launch/worker commands")
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
    elif arguments.command == "run-closure-canaries":
        result = run_closure_canaries(loaded)
    elif arguments.command == "run-v5-census":
        result = run_zero_lean_census(loaded)
    elif arguments.command == "prepare-v5-rehearsal":
        result = prepare_rehearsal_sample(loaded)
    elif arguments.command == "verify-v5-rehearsal-replay":
        assert rehearsal_authorization is not None
        result = verify_rehearsal_replay(loaded, rehearsal_authorization)
    elif arguments.command == "run-v5-rehearsal-audit":
        assert rehearsal_authorization is not None
        result = run_rehearsal_audit(loaded, rehearsal_authorization)
    elif arguments.command == "preflight-v5-rehearsal":
        assert rehearsal_authorization is not None
        result = preflight_rehearsal_launch(loaded, rehearsal_authorization)
    elif arguments.command == "launch-v5-rehearsal":
        assert rehearsal_authorization is not None
        result = launch_detached_rehearsal(loaded, rehearsal_authorization)
    elif arguments.command == "detached-v5-rehearsal-worker":
        assert rehearsal_authorization is not None
        result = run_detached_rehearsal_worker(loaded, rehearsal_authorization)
    elif arguments.command == "prepare-reference-pool":
        result = prepare_reference_pool(loaded)
    elif arguments.command == "materialize-reference-certification-authorization":
        if arguments.reference_certification_authorization is None:
            parser.error("--reference-certification-authorization is required")
        result = materialize_reference_authorization(
            loaded,
            path=arguments.reference_certification_authorization,
        )
    elif arguments.command == "run-reference-certification":
        assert reference_authorization is not None
        result = run_reference_certification(loaded, reference_authorization)
    elif arguments.command == "verify-reference-certification-replay":
        result = verify_reference_replay(loaded)
    elif arguments.command == "verify-global-reference-preflight":
        result = verify_global_reference_preflight(loaded)
    elif arguments.command == "preflight-reference-certification":
        assert reference_authorization is not None
        result = preflight_reference_launch(loaded, reference_authorization)
    elif arguments.command == "launch-reference-certification":
        assert reference_authorization is not None
        result = launch_detached_reference_certification(loaded, reference_authorization)
    elif arguments.command == "reference-certification-health":
        result = reference_certification_health(loaded)
    elif arguments.command == "detached-reference-certification-worker":
        assert reference_authorization is not None
        result = run_detached_reference_certification_worker(loaded, reference_authorization)
    elif arguments.command == "prepare-parallel-rehearsal-path":
        result = prepare_parallel_rehearsal_path(loaded)
    elif arguments.command == "prepare-provider-readiness-v5-2":
        assert provider_loaded is not None
        result = prepare_provider_readiness_v52(provider_loaded)
    elif arguments.command == "preview-provider-authorization-v5-2":
        assert provider_loaded is not None
        readiness_path = provider_loaded.output_root / "readiness/provider_readiness.json"
        result = {
            "authorization_sentence": authorization_sentence_v52(
                provider_loaded, hash_file(readiness_path)
            ),
            "readiness_sha256": hash_file(readiness_path),
            "launch_started": False,
        }
    elif arguments.command == "materialize-provider-authorization-v5-2":
        assert provider_loaded is not None
        result = materialize_provider_authorization_v52(
            provider_loaded, authorization_sentence=arguments.authorization_sentence
        )
    elif arguments.command == "preflight-provider-rehearsal-v5-2":
        assert provider_loaded is not None
        result = preflight_provider_launch_v52(provider_loaded, None)
    elif arguments.command in {
        "launch-provider-rehearsal-v5-2",
        "resume-provider-rehearsal-v5-2",
    }:
        assert provider_loaded is not None and provider_authorization is not None
        result = launch_provider_rehearsal_v52(provider_loaded, provider_authorization)
    elif arguments.command == "provider-rehearsal-v5-2-health":
        assert provider_loaded is not None
        result = provider_rehearsal_health_v52(provider_loaded)
    elif arguments.command == "detached-provider-rehearsal-v5-2-worker":
        assert provider_loaded is not None and provider_authorization is not None
        result = run_detached_provider_rehearsal_v52(provider_loaded, provider_authorization)
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
