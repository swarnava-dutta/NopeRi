"""Entry point for the Naukri apply agent.

Applies recommended jobs first (if enabled), then jobs from configured
search terms. Run via ``Run Noperi.bat``, which resolves ``.venv`` Python.
"""

from src.agents.orchestrator import NaukriApplyOrchestrator

if __name__ == "__main__":
    NaukriApplyOrchestrator().run()
