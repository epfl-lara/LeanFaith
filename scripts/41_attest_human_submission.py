#!/usr/bin/env python3
"""Compatibility entry point for ``leanfaith attest-human-submission``."""

from __future__ import annotations

import sys

from leanfaith.cli.app import app

if __name__ == "__main__":
    app(
        args=["attest-human-submission", *sys.argv[1:]],
        prog_name="41_attest_human_submission.py",
    )
