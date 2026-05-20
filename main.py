"""Compatibility entry point for the Naukri apply agent.

`apply_agent.py` owns the production flow:
- applies recommended jobs first
- then applies jobs from configured search terms
"""

from runpy import run_module


if __name__ == "__main__":
    run_module("apply_agent", run_name="__main__")
