#!/usr/bin/env python3
"""Compatibility entry point for ``leanfaith write-adjudication-queue``."""

from __future__ import annotations

import sys

from leanfaith.cli.app import app

if __name__ == "__main__":
    app(
        args=["write-adjudication-queue", *sys.argv[1:]],
        prog_name="43_write_adjudication_queue.py",
    )
