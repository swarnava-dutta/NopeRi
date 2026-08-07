import sys

from src.agents.easy_apply_agent import EasyApplyAgent
from src.agents.external_link_agent import ExternalLinkAgent
from src.agents.job_ranker import age_label, rank_leads
from src.agents.job_sources import collect_all
from src.agents.job_utils import empty_stats
from src.client.job_client import NaukriJobClient
from src.client.naukri_client import NaukriLoginClient
from src.config import agent_config as config
from src.utils import humanizer


class NaukriApplyOrchestrator:
    """Runs the apply pipeline: collect → sort newest-first → apply.

    Every source is collected into ONE deduped pool before a single apply
    happens. The pool is then ordered newest-first across ALL search terms
    and applied to top-down until the daily limit is hit — so the budget
    goes to the freshest postings rather than being burnt by whichever
    keyword happened to be shuffled first.
    """

    def __init__(self) -> None:
        self.seen_job_ids = set()
        self.totals = empty_stats()
        # Jitter the daily limit so the account doesn't apply to exactly
        # the same number of jobs every single day (a strong bot signal).
        self.daily_limit = humanizer.jitter_int(config.DAILY_APPLY_LIMIT, config.DAILY_LIMIT_JITTER)

    def run(self) -> None:
        # Not redundant with Run Noperi.bat's PYTHONIOENCODING: a bare
        # `python main.py` on a cp1252 console dies on the emoji output.
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

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

        leads = self._collect(job_client)
        leads = self._rank(leads)
        self._apply(easy_apply_agent, leads)

        self._print_summary()

    # ------------------------------------------------------------------
    # Phase 1 — collect
    # ------------------------------------------------------------------

    def _collect(self, job_client) -> list:
        print("\n" + "=" * 52)
        print("📥 PHASE 1 — Collecting jobs")
        print("=" * 52)

        leads = collect_all(job_client, self.seen_job_ids)
        print(f"\n📦 Pool: {len(leads)} unique jobs")
        return leads

    # ------------------------------------------------------------------
    # Phase 2 — order newest-first
    # ------------------------------------------------------------------

    def _rank(self, leads: list) -> list:
        if not leads:
            return leads

        print("\n" + "=" * 52)
        print("🕐 PHASE 2 — Sorting pool (newest first)")
        print("=" * 52)

        ranked = rank_leads(leads)

        if not config.RANK_JOB_POOL:
            print("ℹ️ Sorting disabled by config — keeping collection order.")
            return ranked

        # How the pool breaks down by posting age.
        buckets: dict[str, int] = {}
        for lead in ranked:
            label = age_label(lead.job)
            buckets[label] = buckets.get(label, 0) + 1
        print("🕐 Age spread: " + "  ".join(f"{k}={v}" for k, v in buckets.items()))

        print("\nNewest in the pool:")
        for lead in ranked[:8]:
            print(f"  [{age_label(lead.job):>5}] {lead.job.title} @ {lead.job.company}")
        if len(ranked) > 8:
            print(f"  ... and {len(ranked) - 8} more")

        return ranked

    # ------------------------------------------------------------------
    # Phase 3 — apply
    # ------------------------------------------------------------------

    def _apply(self, easy_apply_agent, leads: list) -> None:
        print("\n" + "=" * 52)
        print("🚀 PHASE 3 — Applying")
        print("=" * 52)

        if not leads:
            print("ℹ️ Nothing collected — nothing to apply to.")
            return

        # Search pushback must not veto the apply phase: they are different
        # endpoints. A 406 recaptcha on search page 3 was killing the whole
        # apply loop on its first iteration (pool collected, 0 attempted).
        # Apply-side 403/429s still count from zero and can still abort.
        humanizer.reset_blocks()

        self.totals = easy_apply_agent.run(leads, self.daily_limit)

    def _print_summary(self) -> None:
        print("\n📊 Run summary")
        print(f"📦 Fetched: {self.totals['found']}")
        print(f"🚀 Attempted: {self.totals['attempted']}")
        print(f"✅ Applied: {self.totals['applied']}")
        print(f"⏭️ Already applied: {self.totals['skipped_applied']}")
        print(f"🚫 Blocked companies: {self.totals['skipped_blocked']}")
        print(f"🙅 Excluded roles: {self.totals['skipped_excluded']}")
        print(f"🧠 Not AI roles (AI judged): {self.totals['skipped_irrelevant']}")

        print(f"👀 Browsed only: {self.totals['skipped_browse']}")
        print(f"📄 External (logged to CSV): {self.totals['skipped_ext']}")
        print(f"⚠️ Failed: {self.totals['failed']}")
