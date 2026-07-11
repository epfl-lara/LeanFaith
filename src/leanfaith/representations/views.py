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
_NESTED_BLOCK_COMMENT = re.compile(r"/-.*?-/", re.DOTALL)
_LINE_COMMENT = re.compile(r"--[^\n]*")
_MODIFIERS = re.compile(r"^\s*(?:protected|private|noncomputable|scoped|local)\s+")
#: A declaration name: a guillemet identifier ``«...»`` (which may contain
#: spaces) or an ordinary whitespace-free token.
_DECL_KEYWORD_NAME = re.compile(r"^\s*(?:theorem|lemma)\s+(?:«[^»]*»|\S+)\s*")
#: The proof tail of a proof-stripped declaration (`:= by sorry` / `:= sorry`)
#: or a benchmark reference statement with an empty body (bare trailing `:=`).
_PROOF_TAIL = re.compile(r"\s*:=\s*(?:by\s+sorry|sorry)?\s*$")


def _strip_nested_block_comments(text: str) -> str:
    """Remove block comments honoring Lean's nesting (``/- a /- b -/ c -/``),
    which a non-greedy regex closes too early."""
    out: list[str] = []
    depth = 0
    i = 0
    while i < len(text):
        if text.startswith("/-", i):
            depth += 1
            i += 2
        elif text.startswith("-/", i) and depth > 0:
            depth -= 1
            i += 2
        else:
            if depth == 0:
                out.append(text[i])
            i += 1
    return "".join(out)


def _strip_leading_attributes(text: str) -> str:
    """Remove leading ``@[...]`` attribute blocks with balanced brackets, so an
    attribute argument that itself contains ``[...]`` does not break the match."""
    i = 0
    n = len(text)
    while True:
        j = i
        while j < n and text[j].isspace():
            j += 1
        if not text.startswith("@[", j):
            return text[i:]
        depth = 0
        k = j + 1
        while k < n:
            if text[k] == "[":
                depth += 1
            elif text[k] == "]":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        if k >= n:
            return text[i:]  # unbalanced; leave as-is
        i = k + 1


def normalize_headless(source: str) -> str | None:
    """Best-effort §13.2 headless view for text lacking a Lean-parsed
    signature (benchmark references): name, proof, comments, and attributes
    removed, whitespace collapsed, for renaming-invariant comparison.

    The primary path uses the elaborator/parser-derived signature instead
    (``TheoremForRepresentation.source_signature``); this string fallback
    handles nested block comments, guillemet names, and nested-bracket
    attributes, but is not string-literal aware (a ``--`` or ``/- -/`` inside
    a string literal is treated as a comment). Returns None if no declaration
    head is found."""
    text = _strip_nested_block_comments(source)
    text = _LINE_COMMENT.sub(" ", text)
    text = _PROOF_TAIL.sub("", text)
    text = _strip_leading_attributes(text)
    text = _MODIFIERS.sub("", text)
    while _MODIFIERS.match(text):
        text = _MODIFIERS.sub("", text)
    without_head, count = _DECL_KEYWORD_NAME.subn("", text, count=1)
    if count == 0:
        return None
    return _WS.sub(" ", without_head).strip() or None


def check_command(imports: str, options_inline: str, full_names: list[str]) -> str:
    """One Command running ``#check @name`` under ``options_inline`` for each
    name — batched so the environment loads once for the whole group."""
    lines = [imports.rstrip("\n")]
    lines.extend(f"{options_inline} #check @{name}" for name in full_names)
    return "\n".join(lines)


#: The name→type separator in #check output: space, colon, then whitespace
#: (which may be a newline when the type wraps to the next line). Matched
#: before whitespace collapse. A declaration name never contains this, and the
#: universe list ``.{u_1, ...}`` has no colon, so the first match is the
#: separator.
_NAME_TYPE_SEP = re.compile(r" :\s")


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
    match = _NAME_TYPE_SEP.search(body)
    if match is None:
        return None
    return _WS.sub(" ", body[match.end() :]).strip() or None


def representation_content_hash(views: dict[str, str | None]) -> str:
    """Deterministic content hash over the view dict (§11.4)."""
    return hash_canonical(views)


def signature_near_dup_hash(signature: str) -> str:
    """Whitespace-collapsed hash of an elaborated signature for near-duplicate
    detection (§19.4). Full-name/explicit pins make this robust to notation."""
    return sha256_hex(_WS.sub(" ", signature).strip().encode("utf-8"))
