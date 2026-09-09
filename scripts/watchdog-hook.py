# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Run the bundled stdlib-only watchdog without installation or provider access."""

import runpy
import sys
from pathlib import Path

if __name__ == "__main__":
    runtime = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "src/omp_tandem/watchdog.py"),
    )
    raise SystemExit(
        runtime["main"](output=sys.stdout if "--stdout" in sys.argv else sys.stderr)
    )
