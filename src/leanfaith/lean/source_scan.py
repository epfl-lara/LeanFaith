"""Small comment-aware scanners for Lean source preambles.

These helpers do not parse Lean declarations. They only recognize real
top-level import commands while respecting nested block comments, line
comments, and string literals. Lean remains the authority for elaboration.
"""

from __future__ import annotations


def scan_lean_line(line: str, block_depth: int) -> tuple[int | None, int]:
    """Return the first top-level code offset and ending block-comment depth."""

    first_code: int | None = None
    in_string = False
    escaped = False
    index = 0
    while index < len(line):
        if block_depth:
            if line.startswith("/-", index):
                block_depth += 1
                index += 2
            elif line.startswith("-/", index):
                block_depth -= 1
                index += 2
            else:
                index += 1
            continue

        if in_string:
            char = line[index]
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue

        if line.startswith("--", index):
            break
        if line.startswith("/-", index):
            block_depth += 1
            index += 2
            continue

        char = line[index]
        if first_code is None and not char.isspace():
            first_code = index
        if char == '"':
            in_string = True
        index += 1

    return first_code, block_depth


def _without_lean_comments(source: str) -> str:
    """Mask Lean comments while preserving code, strings, and newlines."""

    output: list[str] = []
    block_depth = 0
    in_string = False
    escaped = False
    line_comment = False
    index = 0
    while index < len(source):
        if source[index] == "\n":
            output.append("\n")
            line_comment = False
            index += 1
            continue
        if line_comment:
            output.append(" ")
            index += 1
            continue
        if block_depth:
            if source.startswith("/-", index):
                output.extend((" ", " "))
                block_depth += 1
                index += 2
            elif source.startswith("-/", index):
                output.extend((" ", " "))
                block_depth -= 1
                index += 2
            else:
                output.append(" ")
                index += 1
            continue
        if in_string:
            char = source[index]
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if source.startswith("--", index):
            output.extend((" ", " "))
            line_comment = True
            index += 2
            continue
        if source.startswith("/-", index):
            output.extend((" ", " "))
            block_depth = 1
            index += 2
            continue
        char = source[index]
        output.append(char)
        if char == '"':
            in_string = True
        index += 1
    return "".join(output)


def has_top_level_import_family(source: str, module_family: str) -> bool:
    """Whether source contains a real top-level import of a module family.

    Both ``import Mathlib.X`` and module-mode ``public import Mathlib.X`` are
    recognized. Import-like prose inside comments or strings is ignored.
    """

    for line in _without_lean_comments(source).splitlines():
        command = line.strip()
        if not command:
            continue
        if command == "prelude" or command.startswith("prelude "):
            continue
        if command == "module" or command.startswith("module "):
            continue
        if command.startswith("public "):
            command = command.removeprefix("public ").lstrip()
        if not command.startswith("import "):
            # Imports are a file preamble. Once a real non-header command is
            # reached, later import-looking code cannot establish the request
            # environment and must not influence corruption recovery.
            break
        remainder = command.removeprefix("import ").strip()
        if not remainder:
            continue
        for module in remainder.split():
            if module.startswith("--") or module.startswith("/-"):
                break
            if module == module_family or module.startswith(f"{module_family}."):
                return True
    return False
