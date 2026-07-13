"""Verify that every shipped voice profile was installed from the wheel."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path


def main() -> None:
    modules = ("faster_whisper", "pynput", "kokoro_onnx", "openwakeword")
    missing = [name for name in modules if importlib.util.find_spec(name) is None]
    assert not missing, f"missing optional voice modules: {missing}"

    scripts = Path(sys.executable).parent
    suffix = ".exe" if os.name == "nt" else ""
    floor = scripts / f"voco-tts-floor{suffix}"
    assert floor.is_file(), f"missing console script: {floor}"
    subprocess.run([str(floor), "--help"], check=True, stdout=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
