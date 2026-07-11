#!/usr/bin/env python3
"""Phase 2 script (PLAN.md §7.2): run configured source probes.

Thin wrapper over ``leanfaith probe all`` so the numbered-script interface
stays stable; all logic lives in the package.
"""

import subprocess
import sys

if __name__ == "__main__":
    raise SystemExit(
        subprocess.run(
            ["uv", "run", "leanfaith", "probe", *(sys.argv[1:] or ["all"])], check=False
        ).returncode
    )
