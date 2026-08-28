"""Deterministic public-only Argilla fixtures for clean-checkout tests.

The production annotation export is intentionally ignored because it also
contains private linkage artifacts.  Tests that exercise the production
Argilla boundary therefore build a small synthetic repository root containing
only blinded public bundles and the tracked annotation template.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from leanfaith.annotation_support.export import (
    ANNOTATION_GUIDELINE_PATH,
    ANNOTATION_TEMPLATE_PATH,
    ArtifactBinding,
    BlindedAnnotationItemV1,
    BlindedBundleManifestV1,
    LeanDisplayViewsV1,
    PermittedContextV1,
)
from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, sha256_hex

_SLOTS = ("independent_annotator_1", "independent_annotator_2")


@dataclass(frozen=True)
class ArgillaPublicFixture:
    """One temporary repository root and its exact public-bundle registry."""

    repo_root: Path
    bundle_manifests: dict[str, tuple[str, str, str]]


def _item(*, slot: str, index: int) -> BlindedAnnotationItemV1:
    token_digest = sha256_hex(f"argilla-public-test-fixture-v1:{slot}:{index}".encode())
    return BlindedAnnotationItemV1(
        opaque_item_token=f"lf023_blind_item_v1:{token_digest}",
        natural_language_statement=(
            "For every natural number, adding zero leaves the number unchanged."
        ),
        lean_a=LeanDisplayViewsV1(
            headless="(n : Nat) : n + 0 = n",
            signature_pp="∀ n : Nat, n + 0 = n",
            signature_explicit=("∀ (n : Nat), @Eq Nat (@HAdd.hAdd Nat Nat Nat instHAddNat n 0) n"),
        ),
        lean_b=LeanDisplayViewsV1(
            headless="(n : Nat) : 0 + n = n",
            signature_pp="∀ n : Nat, 0 + n = n",
            signature_explicit=("∀ (n : Nat), @Eq Nat (@HAdd.hAdd Nat Nat Nat instHAddNat 0 n) n"),
        ),
        permitted_context=PermittedContextV1(
            minimal_import_text="import Mathlib",
            namespace_text="",
            local_notation_text="",
            required_type_information=(
                "Use each side's explicit signature for elaborated binder and constant types."
            ),
        ),
    )


def build_argilla_public_fixture(
    *,
    repo_root: Path,
    source_repo_root: Path,
) -> ArgillaPublicFixture:
    """Build two 240-item synthetic blinded bundles without private linkage."""

    template_source = source_repo_root / ANNOTATION_TEMPLATE_PATH
    template_target = repo_root / ANNOTATION_TEMPLATE_PATH
    template_target.parent.mkdir(parents=True, exist_ok=True)
    template_target.write_bytes(template_source.read_bytes())
    guideline_source = source_repo_root / ANNOTATION_GUIDELINE_PATH
    guideline_target = repo_root / ANNOTATION_GUIDELINE_PATH
    guideline_target.parent.mkdir(parents=True, exist_ok=True)
    guideline_target.write_bytes(guideline_source.read_bytes())

    registry: dict[str, tuple[str, str, str]] = {}
    fixture_root = Path("tests/fixtures/generated_argilla_public_v1")
    for slot in _SLOTS:
        items = tuple(_item(slot=slot, index=index) for index in range(240))
        bundle_raw = b"".join(
            canonical_json_bytes(item.model_dump(mode="json")) + b"\n" for item in items
        )
        bundle_sha = sha256_hex(bundle_raw)
        bundle_relative = fixture_root / "bundles" / f"{bundle_sha}.jsonl"
        bundle_path = repo_root / bundle_relative
        bundle_path.parent.mkdir(parents=True, exist_ok=True)
        bundle_path.write_bytes(bundle_raw)

        manifest_payload = {
            "schema_version": 1,
            "manifest_kind": "lf023_blinded_annotation_bundle_v1",
            "annotator_slot": slot,
            "bundle_id": f"lf023_blinded_bundle_v1:{bundle_sha}",
            "bundle": ArtifactBinding(
                artifact=bundle_relative.as_posix(),
                sha256=bundle_sha,
            ),
            "item_count": 240,
            "item_schema": "BlindedAnnotationItemV1",
            "blinding_contract": "lf023_reference_aware_minimal_v1",
            "randomized_order": True,
        }
        manifest_id = "lf023_blinded_bundle_manifest_v1:" + hash_canonical(
            {
                "schema": "lf023_blinded_bundle_manifest_v1",
                **{
                    key: (
                        value.model_dump(mode="json")
                        if isinstance(value, ArtifactBinding)
                        else value
                    )
                    for key, value in manifest_payload.items()
                },
            }
        )
        manifest = BlindedBundleManifestV1(
            manifest_id=manifest_id,
            **manifest_payload,  # type: ignore[arg-type]
        )
        manifest_raw = canonical_json_bytes(manifest.model_dump(mode="json"))
        manifest_relative = fixture_root / "manifests" / f"{manifest_id.rsplit(':', 1)[-1]}.json"
        manifest_path = repo_root / manifest_relative
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_bytes(manifest_raw)
        registry[slot] = (
            manifest_relative.as_posix(),
            sha256_hex(manifest_raw),
            manifest_id,
        )

    return ArgillaPublicFixture(
        repo_root=repo_root,
        bundle_manifests=registry,
    )
