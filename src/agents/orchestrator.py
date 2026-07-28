import sys

from src.agents.easy_apply_agent import EasyApplyAgent
from src.agents.external_link_agent import ExternalLinkAgent
from src.agents.job_sources import run_recommended, run_search_term
from src.agents.job_utils import add_stats, empty_stats
from src.client.job_client import NaukriJobClient
from src.client.naukri_client import NaukriLoginClient
from src.config import agent_config as config
from src.utils import humanizer


class NaukriApplyOrchestrator:
    """Coordinates the apply phases in a fixed order: recommended → search."""

    def __init__(self) -> None:
        self.seen_job_ids = set()
        self.totals = empty_stats()
        # Jitter the daily limit so the account doesn't apply to exactly
        # the same number of jobs every single day (a strong bot signal).
        self.daily_limit = humanizer.jitter_int(config.DAILY_APPLY_LIMIT, config.DAILY_LIMIT_JITTER)

    def run(self) -> None:
        self._configure_output()

        # Randomized warm-up so scheduled runs never hit Naukri at the
        # exact same second every day.
        humanizer.session_warmup()

        print("🔐 Logging in...")
        login_client = NaukriLoginClient()
        login_client.login()
        print("✅ Login successful")

        if self.daily_limit != config.DAILY_APPLY_LIMIT:
            print(f"🎲 Daily limit jittered: {config.DAILY_APPLY_LIMIT} → {self.daily_limit}")

        job_client = NaukriJobClient(login_client)
        easy_apply_agent = EasyApplyAgent(job_client, ExternalLinkAgent())

        self._run_recommended(job_client, easy_apply_agent)
        self._run_search_terms(job_client, easy_apply_agent)
        self._print_summary()

    def _run_recommended(self, job_client, easy_apply_agent) -> None:
        if not config.RUN_RECOMMENDED_PHASE:
            print("\n⏭️ Recommended phase skipped by config.")
            return

        add_stats(self.totals, run_recommended(
            job_client, easy_apply_agent, self.seen_job_ids, self._daily_remaining(),
        ))

    def _run_search_terms(self, job_client, easy_apply_agent) -> None:
        if not config.RUN_SEARCH_PHASE:
            print("\n⏭️ Search phase skipped by config.")
            return

        # Shuffle query order each run so the request sequence is never
        # identical between runs.
        queries = config.SEARCH_QUERIES
        if config.SHUFFLE_SEARCH_QUERIES:
            queries = humanizer.shuffled(queries)

        for index, query in enumerate(queries):
            if self._daily_remaining() <= 0:
                print("🛑 Daily apply limit reached. Stopping search.")
                break

            if humanizer.too_many_blocks():
                print("🛑 Too many server blocks this run — aborting to protect the account.")
                break

            # Randomized pause between search terms (skip before first one).
            if index > 0:
                humanizer.human_delay(config.QUERY_DELAY_MIN_SECONDS, config.QUERY_DELAY_MAX_SECONDS)

            add_stats(self.totals, run_search_term(
                job_client, easy_apply_agent, query, self.seen_job_ids, self._daily_remaining(),
            ))

    def _daily_remaining(self) -> int:
        return self.daily_limit - self.totals["applied"]

    def _print_summary(self) -> None:
        print("\n📊 Run summary")
        print(f"📦 Fetched: {self.totals['found']}")
        print(f"🚀 Attempted: {self.totals['attempted']}")
        print(f"✅ Applied: {self.totals['applied']}")
        print(f"⏭️ Already applied: {self.totals['skipped_applied']}")
        print(f"🚫 Blocked companies: {self.totals['skipped_blocked']}")
        print(f"👀 Browsed only: {self.totals['skipped_browse']}")
        print(f"❌ External links: {self.totals['skipped_ext']}")
        print(f"⚠️ Failed: {self.totals['failed']}")

    def _configure_output(self) -> None:
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
