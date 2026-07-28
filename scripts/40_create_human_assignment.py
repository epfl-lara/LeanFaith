#!/usr/bin/env python3
"""Compatibility entry point for ``leanfaith create-human-assignment``."""

from __future__ import annotations

import sys

from leanfaith.cli.app import app

if __name__ == "__main__":
    app(
        args=["create-human-assignment", *sys.argv[1:]],
        prog_name="40_create_human_assignment.py",
    )
