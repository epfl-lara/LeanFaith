"""CLI for the gated ReForm-32B vLLM smoke, replay, and bounded throughput probe."""

from __future__ import annotations

import argparse
import json
import shlex
from collections.abc import Sequence
from importlib import metadata
from pathlib import Path

from leanfaith.config.hashing import hash_file
from leanfaith.sft2b.durable import write_json
from leanfaith.sft2b.pins import verify_runtime_pins
from leanfaith.sft2b.vllm_backend import (
    build_vllm_serve_command,
    profile_endpoint,
    run_vllm_profile,
    summarize_profile,
    verify_openai_server,
    verify_vllm_dependencies,
    visible_devices_csv,
)
from leanfaith.sft2b.vllm_telemetry import TelemetryMonitor


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("launch-command", "run"))
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("configs/sft2b/reform_32b_vllm_v1.json"),
    )
    parser.add_argument("--snapshot-path", type=Path, required=True)
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument(
        "--profile",
        choices=("smoke_dp1_tp2", "probe_dp4_tp2_c8"),
        required=True,
    )
    parser.add_argument("--endpoint-url")
    parser.add_argument("--pass-name", choices=("initial", "restart", "restart_process"))
    parser.add_argument("--server-pid", type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo_root = args.repo_root.resolve()
    helper = repo_root / "src/leanfaith/sft2b/lean_helper.lean"
    pins = verify_runtime_pins(repo_root, helper_path=helper)
    config_path = (repo_root / args.config).resolve()
    backend = verify_vllm_dependencies(
        repo_root,
        config_path=config_path,
        snapshot_path=args.snapshot_path.resolve(),
        release_root=args.release_root.resolve(),
    )
    profile = backend.spec.profiles[args.profile]
    command = build_vllm_serve_command(backend, profile_name=args.profile)
    if args.action == "launch-command":
        print(
            json.dumps(
                {
                    "schema_version": "sft2b_vllm_launch_command_v1",
                    "profile": args.profile,
                    "profile_id": profile.profile_id,
                    "cuda_visible_devices": visible_devices_csv(profile),
                    "command": list(command),
                    "shell_command": shlex.join(command),
                    "backend_config_sha256": backend.config_sha256,
                    "placement_config_sha256": backend.spec.placement_config_sha256,
                    "snapshot_binding_sha256": backend.spec.snapshot_binding_sha256,
                    "repr_pins": pins.to_dict(),
                },
                sort_keys=True,
            )
        )
        return 0
    if args.pass_name is None:
        raise RuntimeError("--pass-name is required for a vLLM run")
    endpoint_url = args.endpoint_url or profile_endpoint(backend, args.profile)
    expected_url = profile_endpoint(backend, args.profile)
    if endpoint_url != expected_url:
        raise RuntimeError(f"endpoint URL must match the frozen profile: {expected_url}")
    server = verify_openai_server(
        endpoint_url,
        served_model_name=backend.spec.served_model_name,
    )
    monitor = TelemetryMonitor(
        endpoint_url=endpoint_url,
        interval_seconds=backend.spec.telemetry_interval_seconds,
        server_pid=args.server_pid,
    )
    monitor.start()
    try:
        result = run_vllm_profile(
            backend,
            profile_name=args.profile,
            endpoint_url=endpoint_url,
        )
    finally:
        monitor.stop()
    receipt_dir = result.root / "receipts"
    telemetry_path = receipt_dir / f"{args.pass_name}_telemetry.jsonl"
    monitor.write(telemetry_path)
    summary = summarize_profile(result)
    summary.update(
        {
            "pass_name": args.pass_name,
            "profile": args.profile,
            "profile_id": profile.profile_id,
            "server": server,
            "telemetry": monitor.summary(),
            "telemetry_path": str(telemetry_path),
            "telemetry_sha256": hash_file(telemetry_path),
            "backend_config_path": str(config_path),
            "backend_config_sha256": backend.config_sha256,
            "placement_config_sha256": backend.spec.placement_config_sha256,
            "snapshot_binding_sha256": backend.spec.snapshot_binding_sha256,
            "model_revision": backend.spec.model_revision,
            "checkpoint_dtype": backend.spec.checkpoint_dtype,
            "quantization": backend.spec.quantization,
            "decoding": backend.placement.decoding,
            "decoding_sha256": backend.placement.decoding_sha256,
            "max_model_len": profile.max_model_len,
            "max_num_seqs": profile.max_num_seqs,
            "gpu_memory_utilization": profile.gpu_memory_utilization,
            "prefix_caching": profile.prefix_caching,
            "data_parallel_size": profile.data_parallel_size,
            "tensor_parallel_size": profile.tensor_parallel_size,
            "visible_devices": profile.visible_devices,
            "launch_command": list(command),
            "versions": {
                "vllm": metadata.version("vllm"),
                "torch": metadata.version("torch"),
                "transformers": metadata.version("transformers"),
                "flash_attn": metadata.version("flash-attn"),
            },
            "repr_pins": pins.to_dict(),
        }
    )
    receipt_path = receipt_dir / f"{args.pass_name}_summary.json"
    write_json(receipt_path, summary)
    print(json.dumps({**summary, "receipt_path": str(receipt_path)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
