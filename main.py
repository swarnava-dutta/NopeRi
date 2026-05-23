"""Compatibility entry point for the Naukri apply agent.

`apply_agent.py` owns the production flow:
- applies recommended jobs first
- then applies jobs from configured search terms
"""

import os
import sys
from pathlib import Path
from runpy import run_module


def _ensure_local_venv_python():
    root = Path(__file__).resolve().parent
    venv_python = root / ".venv" / "Scripts" / "python.exe"
    if not venv_python.exists():
        return

    current = Path(sys.executable).resolve()
    target = venv_python.resolve()
    if current == target:
        return

    os.execv(str(target), [str(target), *sys.argv])


if __name__ == "__main__":
    _ensure_local_venv_python()
    run_module("apply_agent", run_name="__main__")
