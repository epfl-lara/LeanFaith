"""Representation views and pinned pretty-print options (PLAN.md §13, LF-014).

Pure string logic: the ``headless`` cosmetic normalization, parsing the
``#check @name`` elaborator output into an elaborated-signature view, and
content/near-duplicate hashing. The Lean-backed builder lives in
``pipeline.py``.

``signature_pp`` and ``signature_explicit`` are obtained by ``#check @name``
under pinned options because LeanInteract's declaration extractor ignores
ambient ``set_option`` (verified). Options follow §13.4.
"""

from __future__ import annotations

import re

from leanfaith.config.hashing import hash_canonical, sha256_hex

NORMALIZATION_VERSION = "repr_v1"

#: §13.4 pinned options, inline form (``set_option X in`` chains before a
#: ``#check``). signature_pp keeps implicits readable with stable full names;
#: signature_explicit shows every implicit, instance, universe, and coercion.
PP_SIGNATURE_INLINE = "set_option pp.fullNames true in set_option pp.proofs false in"
PP_EXPLICIT_INLINE = (
    "set_option pp.explicit true in "
    "set_option pp.universes true in "
    "set_option pp.fullNames true in "
    "set_option pp.proofs false in"
)

_WS = re.compile(r"\s+")
_BLOCK_COMMENT = re.compile(r"/-.*?-/", re.DOTALL)
_LINE_COMMENT = re.compile(r"--[^\n]*")
#: A declaration head: optional attributes and modifiers, the keyword, and the
#: declaration name — everything the headless view drops.
_DECL_HEAD = re.compile(
    r"^\s*(?:@\[[^\]]*\]\s*)*"
    r"(?:protected\s+|private\s+|noncomputable\s+|scoped\s+|local\s+)*"
    r"(?:theorem|lemma)\s+\S+\s*"
)
#: The proof tail of a proof-stripped declaration (`:= by sorry` / `:= sorry`)
#: or a benchmark reference statement with an empty body (bare trailing `:=`).
_PROOF_TAIL = re.compile(r"\s*:=\s*(?:by\s+sorry|sorry)?\s*$")


def normalize_headless(proof_stripped: str) -> str | None:
    """§13.2 headless view: name, proof, and comments removed, whitespace
    collapsed — leaving the binder+conclusion skeleton for renaming-invariant
    comparison. Returns None if the declaration head cannot be located."""
    text = _BLOCK_COMMENT.sub(" ", proof_stripped)
    text = _LINE_COMMENT.sub(" ", text)
    text = _PROOF_TAIL.sub("", text)
    without_head, count = _DECL_HEAD.subn("", text, count=1)
    if count == 0:
        return None
    return _WS.sub(" ", without_head).strip() or None


def check_command(imports: str, options_inline: str, full_names: list[str]) -> str:
    """One Command running ``#check @name`` under ``options_inline`` for each
    name — batched so the environment loads once for the whole group."""
    lines = [imports.rstrip("\n")]
    lines.extend(f"{options_inline} #check @{name}" for name in full_names)
    return "\n".join(lines)


def parse_check_type(message: str, full_name: str) -> str | None:
    """Extract the type from a ``[@]name[.{univs}] : <type>`` #check message,
    confirming it is the expected declaration. Lean drops the leading ``@``
    when every binder is already explicit, so both forms are accepted.
    Returns the whitespace-collapsed type, or None on mismatch."""
    stripped = message.strip()
    body = stripped[1:] if stripped.startswith("@") else stripped
    if not (
        body == full_name or body.startswith(f"{full_name} ") or body.startswith(f"{full_name}.{{")
    ):
        return None
    separator = body.find(" : ")
    if separator == -1:
        return None
    return _WS.sub(" ", body[separator + 3 :]).strip() or None


def representation_content_hash(views: dict[str, str | None]) -> str:
    """Deterministic content hash over the view dict (§11.4)."""
    return hash_canonical(views)


def signature_near_dup_hash(signature: str) -> str:
    """Whitespace-collapsed hash of an elaborated signature for near-duplicate
    detection (§19.4). Full-name/explicit pins make this robust to notation."""
    return sha256_hex(_WS.sub(" ", signature).strip().encode("utf-8"))
