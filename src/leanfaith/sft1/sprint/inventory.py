"""Lean-free Mathlib theorem inventory and deterministic root ordering.

The scanner reads pinned Mathlib source text, tracks ``namespace``/``section``
nesting, and records every non-private ``theorem``/``lemma`` declaration with
its fully qualified name, module, and statement text.  It never runs Lean:
typed applicability is decided by the engine, and a wrongly qualified name is
a cheap ``root_not_found`` terminal rather than a failure.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, sha256_hex

INVENTORY_SCHEMA_VERSION = 1
_DECL = re.compile(
    r"^(?:@\[[^\]]*\]\s*)*"
    r"(?P<modifiers>(?:(?:private|protected|noncomputable|nonrec|unsafe|partial)\s+)*)"
    r"(?P<kind>theorem|lemma)\s+(?P<name>_root_\.[^\s:({\[⦃]+|[^\s:({\[⦃]+)"
)
_NAMESPACE = re.compile(r"^namespace\s+(?P<name>\S+)\s*$")
_SECTION = re.compile(r"^(?:noncomputable\s+)?section(?:\s+(?P<name>\S+))?\s*$")
_END = re.compile(r"^end(?:\s+(?P<name>\S+))?\s*$")
_COMMAND_START = re.compile(
    r"^(?:@\[|theorem\b|lemma\b|def\b|abbrev\b|instance\b|structure\b|class\b|inductive\b|"
    r"example\b|namespace\b|section\b|end\b|variable\b|open\b|universe\b|noncomputable\b|"
    r"private\b|protected\b|attribute\b|alias\b|macro\b|syntax\b|elab\b|set_option\b|"
    r"/--|scoped\b|local\b|deriving\b|mutual\b|#)"
)
_PLAIN_COMPONENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_'!?]*$")
_MAX_STATEMENT_LINES = 60


@dataclass(frozen=True, slots=True)
class Declaration:
    name: str
    module: str
    path: str
    line: int
    kind: str
    statement: str

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "module": self.module,
            "path": self.path,
            "line": self.line,
            "kind": self.kind,
            "statement": self.statement,
        }


def mask_comments(source: str) -> str:
    """Replace Lean comments with spaces, preserving line structure."""

    out: list[str] = []
    i = 0
    n = len(source)
    depth = 0
    in_string = False
    while i < n:
        ch = source[i]
        if depth > 0:
            if source.startswith("/-", i):
                depth += 1
                out.append("  ")
                i += 2
            elif source.startswith("-/", i):
                depth -= 1
                out.append("  ")
                i += 2
            else:
                out.append("\n" if ch == "\n" else " ")
                i += 1
            continue
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(source[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if source.startswith("/-", i):
            depth = 1
            out.append("  ")
            i += 2
            continue
        if source.startswith("--", i):
            while i < n and source[i] != "\n":
                out.append(" ")
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _bracket_depth_delta(text: str) -> int:
    depth = 0
    for ch in text:
        if ch in "([{⦃⟨":
            depth += 1
        elif ch in ")]}⦄⟩":
            depth -= 1
    return depth


def _statement_text(lines: Sequence[str], start: int) -> str:
    collected: list[str] = []
    depth = 0
    for offset in range(_MAX_STATEMENT_LINES):
        index = start + offset
        if index >= len(lines):
            break
        line = lines[index]
        stripped = line.strip()
        if offset > 0 and (not stripped or stripped.startswith("|") or _COMMAND_START.match(line)):
            break
        cut = _find_assignment(line, depth)
        if cut is not None:
            collected.append(line[:cut])
            break
        collected.append(line)
        depth += _bracket_depth_delta(line)
    return " ".join(part.strip() for part in collected).strip()


def _find_assignment(line: str, depth: int) -> int | None:
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if ch in "([{⦃⟨":
            depth += 1
        elif ch in ")]}⦄⟩":
            depth -= 1
        elif (depth <= 0 and line.startswith(":=", i)) or (
            depth <= 0 and line.startswith(" where", i)
        ):
            return i
        i += 1
    return None


def module_name(relative_path: Path) -> str:
    return ".".join(relative_path.with_suffix("").parts)


def scan_source(source: str, *, module: str, path: str) -> Iterator[Declaration]:
    masked = mask_comments(source)
    lines = masked.split("\n")
    stack: list[tuple[str, str | None]] = []  # (kind, name)
    for index, line in enumerate(lines):
        if not line or line[0].isspace():
            continue
        ns = _NAMESPACE.match(line)
        if ns:
            stack.append(("namespace", ns.group("name")))
            continue
        sec = _SECTION.match(line)
        if sec:
            stack.append(("section", sec.group("name")))
            continue
        end = _END.match(line)
        if end:
            if stack:
                stack.pop()
            continue
        decl = _DECL.match(line)
        if decl is None:
            continue
        modifiers = decl.group("modifiers")
        if "private" in modifiers:
            continue
        raw_name = decl.group("name")
        if raw_name.startswith("_root_."):
            full = raw_name[len("_root_.") :]
        else:
            namespaces = [name for kind, name in stack if kind == "namespace" and name]
            full = ".".join([*namespaces, raw_name])
        if not full or full.endswith("."):
            continue
        yield Declaration(
            name=full,
            module=module,
            path=path,
            line=index + 1,
            kind=decl.group("kind"),
            statement=_statement_text(lines, index),
        )


def scan_mathlib(mathlib_dir: Path) -> list[Declaration]:
    root = mathlib_dir / "Mathlib"
    declarations: list[Declaration] = []
    for path in sorted(root.rglob("*.lean")):
        relative = path.relative_to(mathlib_dir)
        source = path.read_text(encoding="utf-8")
        declarations.extend(
            scan_source(source, module=module_name(relative), path=relative.as_posix())
        )
    return declarations


def lean_name_literal(name: str) -> str:
    """Backtick name literal with guillemet escaping for unusual components."""

    components: list[str] = []
    buffer = ""
    in_guillemet = False
    for ch in name:
        if ch == "«":
            in_guillemet = True
        elif ch == "»":
            in_guillemet = False
        if ch == "." and not in_guillemet:
            components.append(buffer)
            buffer = ""
        else:
            buffer += ch
    components.append(buffer)
    escaped: list[str] = []
    for component in components:
        plain = component
        if plain.startswith("«") and plain.endswith("»"):
            plain = plain[1:-1]
        if _PLAIN_COMPONENT.match(plain):
            escaped.append(plain)
        else:
            escaped.append(f"«{plain}»")
    return "`" + ".".join(escaped)


def git_head(directory: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(directory), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def write_inventory(
    declarations: Iterable[Declaration], out_dir: Path, *, mathlib_revision: str
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = [item.to_dict() for item in declarations]
    body = b"".join(canonical_json_bytes(row) + b"\n" for row in rows)
    inventory_path = out_dir / "inventory.jsonl"
    inventory_path.write_bytes(body)
    names = [str(row["name"]) for row in rows]
    manifest = {
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "mathlib_revision": mathlib_revision,
        "declaration_count": len(rows),
        "unique_name_count": len(set(names)),
        "inventory_sha256": sha256_hex(body),
        "scanner": "leanfaith.sft1.sprint.inventory",
    }
    (out_dir / "manifest.json").write_bytes(canonical_json_bytes(manifest) + b"\n")
    return inventory_path


def load_inventory(path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line:
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
    return rows


@dataclass(frozen=True, slots=True)
class Pool:
    pool_id: str
    module_prefixes: tuple[str, ...]
    weight: int


def pool_members(rows: Sequence[dict[str, object]], pools: Sequence[Pool]) -> dict[str, list[str]]:
    """Assign each unique name to the first pool whose module prefix matches."""

    seen: set[str] = set()
    members: dict[str, list[str]] = {pool.pool_id: [] for pool in pools}
    for row in rows:
        name = str(row["name"])
        module = str(row["module"])
        if name in seen:
            continue
        seen.add(name)
        for pool in pools:
            if any(module == p or module.startswith(p + ".") for p in pool.module_prefixes) or (
                not pool.module_prefixes
            ):
                members[pool.pool_id].append(name)
                break
    return members


def ordered_roots(
    rows: Sequence[dict[str, object]], pools: Sequence[Pool], *, order_salt: str
) -> list[tuple[str, str]]:
    """Deterministic weighted interleave of hash-sorted pool members."""

    members = pool_members(rows, pools)
    queues = {
        pool.pool_id: sorted(
            members[pool.pool_id], key=lambda name: hash_canonical([order_salt, pool.pool_id, name])
        )
        for pool in pools
    }
    positions = dict.fromkeys(queues, 0)
    result: list[tuple[str, str]] = []
    pattern: list[str] = []
    for pool in pools:
        pattern.extend([pool.pool_id] * max(pool.weight, 0))
    if not pattern:
        return result
    while True:
        progressed = False
        for pool_id in pattern:
            queue = queues[pool_id]
            position = positions[pool_id]
            if position < len(queue):
                result.append((queue[position], pool_id))
                positions[pool_id] = position + 1
                progressed = True
        if not progressed:
            break
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mathlib", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    revision = git_head(args.mathlib)
    declarations = scan_mathlib(args.mathlib)
    path = write_inventory(declarations, args.out / revision, mathlib_revision=revision)
    print(
        json.dumps(
            {"inventory": str(path), "declarations": len(declarations), "revision": revision}
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
