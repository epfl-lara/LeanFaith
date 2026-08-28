"""Representation views and pinned pretty-print options (PLAN.md §13, LF-014).

Pure string logic: the ``headless`` cosmetic normalization, parsing the
``#check @name`` elaborator output into an elaborated-signature view, and
content/near-duplicate hashing. The Lean-backed builder lives in
``pipeline.py``.

``signature_pp`` and ``signature_explicit`` are obtained from the elaborated
``ConstantInfo.type`` under ``Options.empty`` so ambient core, Mathlib, and
future extension ``pp.*`` settings cannot change the bytes. The complete
Lean-4.31 core inline profiles remain available for legacy ``#check`` recovery
paths. Options follow §13.4.
"""

from __future__ import annotations

import re

from leanfaith.config.hashing import hash_canonical, sha256_hex

NORMALIZATION_VERSION = "repr_v3"

# Every Lean 4.31 delaborator option that can change serialized signature text
# is reset explicitly. Inline dataset declarations may set ambient ``pp.*``
# options before LeanFaith's checks; pinning only the desired non-default
# values would therefore make the same theorem serialize differently by
# source. Values below are Lean 4.31 defaults except for ``pp.fullNames`` and
# the deliberately stable ``pp.mvars=false``.
_PP_BASE_OPTIONS: tuple[tuple[str, str], ...] = (
    ("pp.maxSteps", "5000"),
    ("pp.all", "false"),
    ("pp.raw", "false"),
    ("pp.raw.showInfo", "false"),
    ("pp.raw.maxDepth", "32"),
    ("pp.rawOnError", "false"),
    ("pp.oneline", "false"),
    ("pp.exprSizes", "false"),
    ("pp.macroStack", "false"),
    ("pp.notation", "true"),
    ("pp.parens", "false"),
    ("pp.unicode", "true"),
    ("pp.unicode.fun", "false"),
    ("pp.match", "true"),
    ("pp.sorrySource", "false"),
    ("pp.coercions", "true"),
    ("pp.coercions.types", "false"),
    ("pp.fullNames", "true"),
    ("pp.privateNames", "false"),
    ("pp.sanitizeNames", "true"),
    ("pp.inaccessibleNames", "true"),
    ("pp.auxDecls", "false"),
    ("pp.implementationDetailHyps", "false"),
    ("pp.showLetValues", "false"),
    ("pp.showLetValues.threshold", "0"),
    ("pp.showLetValues.tactic.threshold", "255"),
    ("pp.funBinderTypes", "false"),
    ("pp.piBinderTypes", "true"),
    ("pp.piBinderNames", "false"),
    ("pp.piBinderNames.hygienic", "false"),
    ("pp.foralls", "true"),
    ("pp.letVarTypes", "false"),
    ("pp.natLit", "false"),
    ("pp.numericTypes", "false"),
    ("pp.mdata", "false"),
    ("pp.instantiateMVars", "true"),
    ("pp.mvars", "false"),
    ("pp.mvars.levels", "true"),
    ("pp.mvars.anonymous", "true"),
    ("pp.mvars.withType", "false"),
    ("pp.mvars.delayed", "false"),
    ("pp.fvars.anonymous", "true"),
    ("pp.beta", "false"),
    ("pp.structureInstances", "true"),
    ("pp.structureInstances.flatten", "true"),
    ("pp.structureInstances.defaults", "false"),
    ("pp.fieldNotation", "true"),
    ("pp.fieldNotation.generalized", "true"),
    ("pp.structureInstanceTypes", "false"),
    ("pp.safeShadowing", "true"),
    ("pp.tagAppFns", "false"),
    ("pp.proofs", "false"),
    ("pp.proofs.withType", "false"),
    ("pp.proofs.threshold", "0"),
    ("pp.instances", "true"),
    ("pp.instanceTypes", "false"),
    ("pp.deepTerms", "false"),
    ("pp.deepTerms.threshold", "50"),
    ("pp.motives.pi", "true"),
    ("pp.motives.nonConst", "false"),
    ("pp.motives.all", "false"),
    ("pp.analyze", "false"),
    ("pp.analyze.checkInstances", "false"),
    ("pp.analyze.typeAscriptions", "true"),
    ("pp.analyze.trustSubst", "false"),
    ("pp.analyze.trustOfNat", "true"),
    ("pp.analyze.trustOfScientific", "true"),
    ("pp.analyze.trustSubtypeMk", "true"),
    ("pp.analyze.trustId", "true"),
    ("pp.analyze.trustKnownFOType2TypeHOFuns", "true"),
    ("pp.analyze.omitMax", "true"),
    ("pp.analyze.knowsType", "true"),
    ("pp.analyze.explicitHoles", "false"),
)


def _pp_inline_profile(*, explicit: bool, universes: bool) -> str:
    options = (
        *_PP_BASE_OPTIONS,
        ("pp.explicit", str(explicit).lower()),
        ("pp.universes", str(universes).lower()),
    )
    return " ".join(f"set_option {name} {value} in" for name, value in options)


#: §13.4 fully pinned inline profiles. ``signature_pp`` keeps implicits
#: readable; ``signature_explicit`` exposes implicit arguments and universes.
PP_SIGNATURE_INLINE = _pp_inline_profile(explicit=False, universes=False)
PP_EXPLICIT_INLINE = _pp_inline_profile(explicit=True, universes=True)

_WS = re.compile(r"\s+")
_PP_UNIVERSE_PLACEHOLDER = re.compile(r"\bu_\d+\b")
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
    return collapse_lean_whitespace(body[match.end() :]) or None


def collapse_lean_whitespace(text: str) -> str:
    """Collapse layout whitespace without changing quoted Lean tokens.

    Lean's pretty printer wraps long types across lines. A plain regex collapse
    also changes string literals and guillemet identifiers, turning
    ``"a  b"`` or ``«name  with  spaces»`` into different Lean syntax.
    This small scanner preserves those regions byte-for-byte while
    canonicalizing layout outside them.
    """

    output: list[str] = []
    pending_space = False
    in_string = False
    in_guillemet = False
    escaped = False
    for char in text.strip():
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if in_guillemet:
            output.append(char)
            if char == "»":
                in_guillemet = False
            continue
        if char.isspace():
            pending_space = True
            continue
        if pending_space and output:
            output.append(" ")
        pending_space = False
        output.append(char)
        if char == '"':
            in_string = True
        elif char == "«":
            in_guillemet = True
    return "".join(output)


def representation_content_hash(views: dict[str, object]) -> str:
    """Deterministic content hash over the view dict (§11.4)."""
    return hash_canonical(views)


def signature_near_dup_hash(signature: str) -> str:
    """Whitespace-collapsed hash of an elaborated signature for near-duplicate
    detection (§19.4). Full-name/explicit pins make this robust to notation."""
    return sha256_hex(_WS.sub(" ", signature).strip().encode("utf-8"))


def normalize_pp_universe_placeholders(signature: str) -> str:
    """Alpha-normalize Lean-generated ``u_<n>`` names in pretty output.

    ``#check`` chooses fresh numeric suffixes independently in each command,
    so raw ``signature_explicit`` text is not stable across otherwise
    equivalent name-based and inline inspections. Only the generated
    ``u_<n>`` spelling is rewritten; user universe names and universe
    expression structure are preserved.
    """

    names: dict[str, str] = {}

    def replace(match: re.Match[str]) -> str:
        token = match.group(0)
        if token not in names:
            names[token] = f"u_{len(names)}"
        return names[token]

    return _PP_UNIVERSE_PLACEHOLDER.sub(replace, signature)
