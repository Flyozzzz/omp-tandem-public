# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Dependency-preparing MCP entrypoint; launch with isolated Python via uv."""

import sys
from pathlib import Path


def run():
    # Resolve only trusted bootstrap code before entering the installed runtime.
    root = Path(__file__).resolve().parent
    sys.path.insert(0, str(root / "src"))
    from omp_tandem.bootstrap import main

    return main(root)


if __name__ == "__main__":
    raise SystemExit(run())
