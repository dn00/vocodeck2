"""Install exactly one built Voco wheel into a target interpreter or venv."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("target")
    parser.add_argument("--create", action="store_true")
    parser.add_argument("--extras", default="")
    args = parser.parse_args()

    wheels = sorted(Path("dist").glob("voco-*.whl"))
    if len(wheels) != 1:
        raise SystemExit(f"expected exactly one wheel in dist, found {len(wheels)}")
    if args.create:
        subprocess.run(["uv", "venv", args.target], check=True)
    package = str(wheels[0]) + (f"[{args.extras}]" if args.extras else "")
    subprocess.run(
        ["uv", "pip", "install", "--python", args.target, package], check=True
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
