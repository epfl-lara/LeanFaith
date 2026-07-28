#!/usr/bin/env python3
"""Compatibility entry point for ``leanfaith write-annotation-agreement``."""

from __future__ import annotations

import sys

from leanfaith.cli.app import app

if __name__ == "__main__":
    app(
        args=["write-annotation-agreement", *sys.argv[1:]],
        prog_name="42_write_annotation_agreement.py",
    )
