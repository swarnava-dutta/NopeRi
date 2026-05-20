import sys

from src.agents.easy_apply_agent import EasyApplyAgent
from src.agents.external_link_agent import ExternalLinkAgent
from src.agents.job_utils import add_stats, empty_stats
from src.agents.recommended_agent import RecommendedJobAgent
from src.agents.search_term_agent import SearchTermApplyAgent
from src.client.job_client import NaukriJobClient
from src.client.naukri_client import NaukriLoginClient
from src.config import agent_config as config


class NaukriApplyOrchestrator:
    """Coordinates plain Python agents in a fixed, resumable phase order."""

    def __init__(self) -> None:
        self.seen_job_ids = set()
        self.totals = empty_stats()

    def run(self) -> None:
        self._configure_output()

        print("🔐 Logging in...")
        login_client = NaukriLoginClient()
        login_client.login()
        print("✅ Login successful")

        job_client = NaukriJobClient(login_client)
        external_link_agent = ExternalLinkAgent()
        easy_apply_agent = EasyApplyAgent(job_client, external_link_agent)

        self._run_recommended(job_client, easy_apply_agent)
        self._run_search_terms(job_client, easy_apply_agent)
        self._print_summary()

    def _run_recommended(self, job_client, easy_apply_agent) -> None:
        if not config.RUN_RECOMMENDED_PHASE:
            print("\n⏭️ Recommended phase skipped by config.")
            return

        recommended_agent = RecommendedJobAgent(
            job_client=job_client,
            easy_apply_agent=easy_apply_agent,
            seen_job_ids=self.seen_job_ids,
        )
        add_stats(self.totals, recommended_agent.run(self._daily_remaining()))

    def _run_search_terms(self, job_client, easy_apply_agent) -> None:
        if not config.RUN_SEARCH_PHASE:
            print("\n⏭️ Search phase skipped by config.")
            return

        for query in config.SEARCH_QUERIES:
            if self._daily_remaining() <= 0:
                print("🛑 Daily apply limit reached. Stopping search.")
                break

            search_agent = SearchTermApplyAgent(
                job_client=job_client,
                easy_apply_agent=easy_apply_agent,
                query=query,
                seen_job_ids=self.seen_job_ids,
            )
            add_stats(self.totals, search_agent.run(self._daily_remaining()))

    def _daily_remaining(self) -> int:
        return config.DAILY_APPLY_LIMIT - self.totals["applied"]

    def _print_summary(self) -> None:
        print("\n📊 Run summary")
        print(f"📦 Fetched: {self.totals['found']}")
        print(f"🚀 Attempted: {self.totals['attempted']}")
        print(f"✅ Applied: {self.totals['applied']}")
        print(f"⏭️ Already applied: {self.totals['skipped_applied']}")
        print(f"❌ External links: {self.totals['skipped_ext']}")
        print(f"⚠️ Failed: {self.totals['failed']}")

    def _configure_output(self) -> None:
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

