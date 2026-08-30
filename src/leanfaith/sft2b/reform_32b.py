"""Verification of the prepared ReForm-32B placement contract."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, cast

from leanfaith.config.hashing import hash_canonical, hash_file
from leanfaith.sft2b.formalizer import FormalizerConfig, FormalizerError, SlotSpec
from leanfaith.sft2b.schemas import CandidateOrigin, CandidateSlot


def _git_blob_sha1(path: Path) -> str:
    payload = path.read_bytes()
    return hashlib.sha1(f"blob {len(payload)}\0".encode("ascii") + payload).hexdigest()


def load_reform_32b_config(
    repo_root: Path, *, placement_path: Path, snapshot_path: Path
) -> tuple[FormalizerConfig, int]:
    """Fail closed unless the downloaded 65.5-GB snapshot matches every remote object pin."""

    raw = json.loads(placement_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != "sft2b_reform_32b_placement_v1":
        raise FormalizerError("unsupported ReForm-32B placement config")
    value = cast(dict[str, Any], raw)
    if value.get("status") != "waiting_compute":
        raise FormalizerError("ReForm-32B placement status drifted")
    revision = str(value["model_revision"])
    if snapshot_path.name != revision or not snapshot_path.is_dir():
        raise FormalizerError("ReForm-32B snapshot/revision mismatch")
    raw_files = cast(list[dict[str, object]], value["remote_files"])
    expected_names = {str(item["path"]) for item in raw_files}
    observed_names = {
        str(item.relative_to(snapshot_path)) for item in snapshot_path.rglob("*") if item.is_file()
    }
    if observed_names != expected_names:
        raise FormalizerError("ReForm-32B snapshot file set drifted")
    total_bytes = 0
    binding: dict[str, dict[str, object]] = {}
    for item in raw_files:
        name = str(item["path"])
        path = snapshot_path / name
        expected_size = int(cast(int, item["size"]))
        if path.stat().st_size != expected_size:
            raise FormalizerError(f"ReForm-32B file size mismatch: {name}")
        kind = str(item["hash_kind"])
        expected_hash = str(item["hash"])
        if kind == "sha256":
            observed_hash = hash_file(path)
        elif kind == "git_blob_sha1":
            observed_hash = _git_blob_sha1(path)
        else:
            raise FormalizerError(f"unsupported ReForm-32B hash kind: {kind}")
        if observed_hash != expected_hash:
            raise FormalizerError(f"ReForm-32B content hash mismatch: {name}")
        total_bytes += expected_size
        binding[name] = {"size": expected_size, "hash_kind": kind, "hash": expected_hash}
    if total_bytes != int(value["repository_bytes"]):
        raise FormalizerError("ReForm-32B repository byte total drifted")
    prompt_path = repo_root / str(value["prompt_path"])
    prompt_hash = hash_file(prompt_path)
    if prompt_hash != value["prompt_sha256"]:
        raise FormalizerError("ReForm-32B prompt hash mismatch")
    prompt = prompt_path.read_text(encoding="utf-8")
    if prompt.count("{{NL}}") != 1 or "theorem sft2b_candidate" not in prompt:
        raise FormalizerError("ReForm-32B theorem-signature prompt contract drifted")
    model_config = json.loads((snapshot_path / "config.json").read_text(encoding="utf-8"))
    if not isinstance(model_config, dict) or model_config.get("architectures") != [
        "Qwen3ForCausalLM"
    ]:
        raise FormalizerError("unexpected ReForm-32B architecture")
    if model_config.get("torch_dtype") != "bfloat16":
        raise FormalizerError("unexpected ReForm-32B checkpoint dtype")
    raw_slots = cast(list[dict[str, int | str]], value["candidate_slots"])
    slots = tuple(
        SlotSpec(slot=CandidateSlot(str(item["slot"])), seed=int(item["seed"]))
        for item in raw_slots
    )
    if len(slots) != 4 or {item.slot for item in slots} != set(CandidateSlot):
        raise FormalizerError("ReForm-32B must use all four matched slots")
    decoding = cast(dict[str, object], value["decoding"])
    expected_decoding = {
        "do_sample": True,
        "max_new_tokens": 4096,
        "temperature": 0.6,
        "top_k": 20,
        "top_p": 0.95,
        "repetition_penalty": 1.0,
        "use_cache": True,
    }
    if decoding != expected_decoding:
        raise FormalizerError("ReForm-32B decoding is not matched to the 8B smoke")
    minimum_vram = int(value["hardware"]["minimum_vram_bytes"])
    return (
        FormalizerConfig(
            model_id=str(value["model_id"]),
            model_revision=revision,
            origin=CandidateOrigin.REFORM_32B,
            staging_subdir="reform_32b",
            snapshot_path=snapshot_path,
            snapshot_files={name: str(item["hash"]) for name, item in binding.items()},
            prompt_path=prompt_path,
            prompt_sha256=prompt_hash,
            extraction_contract="final_theorem_signature_v1",
            slots=slots,
            decoding=decoding,
            decoding_sha256=hash_canonical(decoding),
            dtype="bfloat16",
            device="cuda:0",
            trust_remote_code=False,
            local_files_only=True,
            staging_root=Path(str(value["staging_root"])),
            owner_session=str(value["owner_session"]),
            config_sha256=hash_file(placement_path),
            snapshot_binding_sha256=hash_canonical(binding),
        ),
        minimum_vram,
    )
