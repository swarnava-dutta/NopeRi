from src.agents.job_ranker import age_label, is_excluded_title
from src.agents.job_store import load_job_ids, save_applied_job
from src.agents.job_utils import empty_stats, job_label, plain_text
from src.config import agent_config as config
from src.exceptions.exceptions import NaukriAuthError
from src.utils import humanizer
from src.utils.ai_role_filter import is_relevant_role


class EasyApplyAgent:
    """Applies Naukri easy-apply jobs.

    External (company-site) jobs are never part of the apply list — they are
    written to external_jobs.csv and dropped. They cost no daily budget, no
    apply attempt, and no fatigue.
    """

    def __init__(self, job_client, external_link_agent) -> None:
        self.job_client = job_client
        self.external_link_agent = external_link_agent
        self.applied_job_ids = load_job_ids(config.APPLIED_JOBS_CSV)

    @staticmethod
    def _is_blocked_company(job) -> bool:
        company = (job.company or "").lower()
        return any(blocked in company for blocked in config.BLOCKED_COMPANIES)

    # ------------------------------------------------------------------
    # Filtering
    # ------------------------------------------------------------------

    def filter_applicable(self, leads: list, stats: dict) -> list:
        """Drop everything we must not apply to, before any apply happens.

        External jobs found here are documented straight to the CSV — they
        never enter the apply list, so they can't eat the daily budget.
        """
        pending = []

        for lead in leads:
            job = lead.job

            if job.job_id in self.applied_job_ids:
                stats["skipped_applied"] += 1
                print(f"⏭️ Already applied: {job_label(job)}")
            elif self._is_blocked_company(job):
                stats["skipped_blocked"] += 1
                print(f"🚫 Blocked company: {job_label(job)}")
            elif is_excluded_title(job):
                stats["skipped_excluded"] += 1
                print(f"🙅 Excluded role: {job_label(job)}")
            elif job.external is True:
                # The listing itself told us it's a company-site apply, so we
                # can file it without spending a job-details request on it.
                stats["skipped_ext"] += 1
                self.external_link_agent.document(job, lead.source)
                print(f"📄 External (listing) → CSV: {job_label(job)}")
            else:
                pending.append(lead)

        return pending

    @staticmethod
    def _role_verdict(job, description: str) -> bool | None:
        """Is this an AI/ML role? True / False / None when there's no opinion.

        Deliberately called from the apply loop rather than the bulk filter:

        1. It runs only on jobs we are actually about to apply to, so the
           number of paid calls is bounded by the daily budget instead of by
           the (much larger) pool size.
        2. By then the job-details response has given us the real job
           description. Search listings carry almost no description, and a
           title alone can't tell an AI-focused "Software Engineer" apart
           from a generic one.

        None (filter disabled, unkeyed, or tripped) is passed through rather
        than collapsed into False: a missing opinion must never silently
        shrink the apply pool, and callers need to tell "not AI" apart from
        "don't know".
        """
        return is_relevant_role(
            title=job.title,
            company=job.company,
            tags=job.tags,
            description=description or job.description,
        )

    # ------------------------------------------------------------------
    # Apply
    # ------------------------------------------------------------------

    def run(self, leads: list, daily_remaining: int) -> dict:
        """Apply down the newest-first pool until the budget runs out."""
        stats = empty_stats(found=len(leads))

        pending = self.filter_applicable(leads, stats)

        if not pending:
            print("\nℹ️ No new jobs to apply.")
            return stats

        if daily_remaining <= 0:
            print("🛑 Daily apply limit reached.")
            return stats

        # Humans don't work strictly top-to-bottom through a result list —
        # let each job drift a few positions without losing the ranking.
        pending = humanizer.light_shuffle(pending, config.JOB_ORDER_DRIFT)

        print(f"\n🚀 Applying to {len(pending)} jobs (budget: {daily_remaining})")

        for lead in pending:
            if stats["applied"] >= daily_remaining:
                print("🛑 Daily apply limit reached.")
                break

            # Abort the run entirely if the server keeps pushing back —
            # continuing turns a soft block into a real ban.
            if humanizer.too_many_blocks():
                print("🛑 Too many server blocks this run — stopping to protect the account.")
                break

            applied_or_browsed = self._apply_one(lead, stats)

            # External jobs are pure bookkeeping — no reading, no pause, no
            # fatigue. Only real interactions look like human activity.
            if not applied_or_browsed:
                continue

            humanizer.note_job()  # feeds the session-fatigue slow-down

            # Randomized human-like pause between applies, plus occasional
            # long "walked away" breaks.
            humanizer.human_delay(config.APPLY_DELAY_MIN_SECONDS, config.APPLY_DELAY_MAX_SECONDS)
            humanizer.maybe_long_break()

        return stats

    def _apply_one(self, lead, stats: dict) -> bool:
        """Returns True when the job was genuinely interacted with."""
        job = lead.job
        label_text = job_label(job)

        try:
            return self._apply_core(lead, stats, label_text)
        except Exception as exc:
            # A rotated/expired nauk_at mid-run is a stale credential, not a
            # problem with the job — refresh the token and retry once.
            #
            # Trigger on the exception TYPE, not on message text. Matching the
            # literal "invalid user" meant every 403 whose body said anything
            # else ("Auth failed", an empty message, a WAF page) skipped the
            # refresh and burnt the job outright — 8 such lines in
            # logs/noperi_hidden.log. refresh_auth() self-limits: it returns
            # False unless it actually got a DIFFERENT token, so a 429 or a
            # genuine permission error still can't cause a pointless retry.
            if isinstance(exc, NaukriAuthError) and self.job_client.refresh_auth():
                print(f"🔄 Session token refreshed — retrying: {label_text}")
                try:
                    return self._apply_core(lead, stats, label_text)
                except Exception as retry_exc:
                    exc = retry_exc
            stats["failed"] += 1
            print(f"⚠️ Failed: {label_text} | {exc}")
            return True

    def _apply_core(self, lead, stats: dict, label_text: str) -> bool:
        job = lead.job
        source = lead.source

        details = self.job_client.get_job_details(job.job_id)

        # Only the details call can catch externals the listing didn't flag.
        # Document and move on — never an apply attempt, never any budget.
        if self.job_client.is_external(details):
            stats["skipped_ext"] += 1
            self.external_link_agent.document(job, source)
            print(f"📄 External → CSV: {label_text}")
            return False

        # Human "reads" the job description before hitting apply —
        # longer JDs take proportionally longer. Stripped of HTML first, so
        # the pause tracks the words and the role filter judges the words.
        jd_text = plain_text(
            (details.get("job") or {}).get("description") or job.description or ""
        )
        humanizer.reading_pause(len(jd_text))

        # Now that the real JD is in hand, let the model confirm this is
        # actually an AI/ML role. Keyword search returns plenty of QA,
        # C#, and non-engineering listings that no blocklist would catch.
        # Costs no apply budget — it's the same as reading and walking away.
        verdict = self._role_verdict(job, jd_text)
        if verdict is False:
            stats["skipped_irrelevant"] += 1
            print(f"🧠 Not an AI role: {label_text}")
            return True

        # Browse-only exists purely as an anti-ban signal (humans open some
        # jobs and walk away; bots apply to 100% of what they view). A
        # CONFIRMED AI role must never be spent on it — the listings the
        # filter just rejected above are already exactly that signal, opened
        # and abandoned, so the pattern is covered for free.
        #
        # It still fires when the filter has no opinion (verdict None:
        # disabled, unkeyed, or tripped). Otherwise turning the filter off
        # would apply to 100% of everything we open.
        if verdict is None and humanizer.window_shopping():
            stats["skipped_browse"] += 1
            print(f"👀 Browsed only: {label_text}")
            return True

        mandatory = job.tags[:config.MANDATORY_SKILL_COUNT] if job.tags else []
        optional = (
            job.tags[config.MANDATORY_SKILL_COUNT:]
            if len(job.tags) > config.MANDATORY_SKILL_COUNT
            else []
        )

        stats["attempted"] += 1
        result = self.job_client.apply_job(
            job,
            mandatory_skills=mandatory,
            optional_skills=optional,
            source=source,
        )

        job_result = (result.get("jobs") or [{}])[0]
        questionnaire_answers = []
        if job_result.get("questionnaire"):
            # Humans take time to fill in a questionnaire.
            humanizer.human_delay(2.0, 8.0)
            sid = humanizer.generate_sid()
            questionnaire_result = self.job_client.handle_static_questionnaire_and_apply(
                job,
                questionnaire=job_result["questionnaire"],
                sid=sid,
                mandatory_skills=mandatory,
                optional_skills=optional,
                source=source,
            )
            questionnaire_answers = questionnaire_result.get("_questionnaire_answers") or []
            if questionnaire_result.get("success") is False:
                error = questionnaire_result.get("error") or "unknown questionnaire error"
                raise RuntimeError(f"Questionnaire apply failed: {error}")

        save_applied_job(job, questionnaire_answers=questionnaire_answers)
        self.applied_job_ids.add(job.job_id)
        stats["applied"] += 1

        print(f"✅ Applied: {label_text} ({age_label(job)})")
        return True
