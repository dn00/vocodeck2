"""Render the README status block from docs/status.json."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATUS = ROOT / "docs" / "status.json"
README = ROOT / "README.md"
START = "<!-- generated-status:start -->"
END = "<!-- generated-status:end -->"


def render(data: dict) -> str:
    tests = data["tests"]
    verified = "\n".join(f"- {item}" for item in data["verified"])
    limitations = "\n".join(f"- {item}" for item in data["limitations"])
    return (
        f"{START}\n"
        f"Core state: **{data['core_state']}** (as of {data['as_of']}).\n\n"
        f"Automated coverage: {tests['pytest']} pytest tests, "
        f"{tests['javascript']} JavaScript unit tests, and "
        f"{tests['browser_e2e']} real-browser end-to-end test.\n\n"
        f"Verified:\n\n{verified}\n\n"
        f"Remaining boundaries:\n\n{limitations}\n"
        f"{END}"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    data = json.loads(STATUS.read_text(encoding="utf-8"))
    expected = render(data)
    current = README.read_text(encoding="utf-8")
    start = current.find(START)
    end = current.find(END)
    if start < 0 or end < start:
        raise SystemExit("README generated status markers are missing")
    actual = current[start : end + len(END)]
    if args.check:
        if actual != expected:
            raise SystemExit("README status is stale; run scripts/gen_status.py")
        return 0
    README.write_text(current[:start] + expected + current[end + len(END) :])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
