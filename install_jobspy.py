#!/usr/bin/env python3
"""
install_jobspy.py — install python-jobspy without pip trying to build numpy
from source.

python-jobspy's published wheel pins numpy==1.26.3 exactly. That version has
no prebuilt wheel for newer Python releases (3.13+) or for some Linux setups,
so a plain `pip install python-jobspy` falls back to compiling numpy from
source — which then fails immediately if no C compiler (cc/gcc/clang) is on
PATH:

    ERROR: Unable to detect GNU compiler type

The fix isn't a newer numpy — pip will always try to satisfy jobspy's exact
pin. Instead: install jobspy's other dependencies first (letting pip pick
whatever prebuilt wheel already exists for numpy/pandas on this platform),
then install python-jobspy itself with --no-deps so it can't drag the pinned
version back in.

Usage:
    python install_jobspy.py
"""

from __future__ import annotations

import subprocess
import sys

DEPS = [
    "numpy", "pandas", "requests", "beautifulsoup4", "pydantic",
    "tls-client", "markdownify", "regex",
]


def pip(*args: str) -> None:
    cmd = [sys.executable, "-m", "pip", *args]
    print(f"$ {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


def main() -> None:
    pip("install", "-U", *DEPS)
    pip("install", "--no-deps", "-U", "python-jobspy")
    try:
        import jobspy  # noqa: F401
    except ImportError as exc:
        print(f"\ninstalled, but 'import jobspy' still fails: {exc}", file=sys.stderr)
        sys.exit(1)
    print("\npython-jobspy installed and importable.")


if __name__ == "__main__":
    main()
