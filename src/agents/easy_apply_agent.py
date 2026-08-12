from src.agents.job_ranker import age_label, is_excluded_title
from src.agents.job_store import load_job_ids, save_applied_job
from src.agents.job_utils import empty_stats, job_label, plain_text
from src.client.naukri_client import SessionRecovery
from src.config import agent_config as config
from src.exceptions.exceptions import NaukriAuthError
from src.utils import humanizer
from src.utils.ai_role_filter import is_relevant_role
from src.utils.apply_response import ApplyStatus, normalize_job_id, parse_apply_response


class EasyApplyAgent:
    """Applies Naukri easy-apply jobs.

    External (company-site) jobs are never part of the apply list — they are
    written to external_jobs.csv and dropped. They cost no daily budget, no
    apply attempt, and no fatigue.
    """

    def __init__(
        self,
        job_client,
        external_link_agent,
        known_applied_job_ids: set[str] | None = None,
    ) -> None:
        self.job_client = job_client
        self.external_link_agent = external_link_agent
        self.applied_job_ids = load_job_ids(config.APPLIED_JOBS_CSV)
        self.applied_job_ids.update(
            normalized
            for job_id in (known_applied_job_ids or set())
            if (normalized := normalize_job_id(job_id))
        )
        self._stop_run = False
        self._consecutive_auth_failures = 0

    @staticmethod
    def _is_blocked_company(job) -> bool:
        company = (job.company or "").lower()
        return any(blocked in company for blocked in config.BLOCKED_COMPANIES)

    def _document_external(
        self,
        job,
        source: str,
        stats: dict,
        label_text: str,
        origin: str = "",
    ) -> None:
        """Record one external encounter with truthful write/update counters."""
        stats["skipped_ext"] += 1
        try:
            status = self.external_link_agent.document(job, source)
        except Exception as exc:
            stats["external_unsaved"] += 1
            print(f"⚠️ External detected but CSV write failed: {label_text} | {exc}")
            return

        context = f" ({origin})" if origin else ""
        if status == "written":
            stats["external_written"] += 1
            print(f"📄 External{context} → CSV: {label_text}")
        elif status == "updated":
            stats["external_updated"] += 1
            print(f"🔗 External link updated{context} → CSV: {label_text}")
        elif status == "duplicate":
            stats["external_duplicate"] += 1
            print(f"⏭️ External already documented{context}: {label_text}")
        else:
            stats["external_unsaved"] += 1
            print(f"⚠️ External detected but not saved ({status or 'invalid'}): {label_text}")

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
                self._document_external(
                    job,
                    lead.source,
                    stats,
                    job_label(job),
                    origin="listing",
                )
            else:
                pending.append(lead)

        return pending

    @staticmethod
    def _role_verdict(job, description: str) -> bool | None:
        """Is this a target AI/ML/GenAI role? True / False / None if unknown.

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
        self._stop_run = False
        self._consecutive_auth_failures = 0

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

        print(
            f"\n🔎 Reviewing {len(pending)} candidate jobs "
            f"(apply budget: {daily_remaining})"
        )

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

            if self._stop_run:
                break

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
            # An apply-auth rejection may mean a rotated token, a dead login,
            # or Naukri rejecting only this job while the account stays valid.
            # Trigger on exception type, then distinguish three outcomes:
            # a rotated token gets one retry; an unchanged but dashboard-valid
            # token means this job request alone was rejected; a dead session
            # stops the run before dozens more apply requests fail.
            if isinstance(exc, NaukriAuthError):
                recovery = self.job_client.refresh_auth()
                if recovery is True:  # compatibility with older clients
                    recovery = SessionRecovery.REFRESHED
                elif recovery is False:
                    recovery = SessionRecovery.FAILED

                if recovery is SessionRecovery.REFRESHED:
                    print(f"🔄 Session token refreshed — retrying: {label_text}")
                    try:
                        result = self._apply_core(lead, stats, label_text)
                        self._consecutive_auth_failures = 0
                        return result
                    except Exception as retry_exc:
                        exc = retry_exc
                        if isinstance(retry_exc, NaukriAuthError):
                            self._consecutive_auth_failures += 1
                            self._stop_run = True
                        else:
                            self._consecutive_auth_failures = 0
                elif recovery is SessionRecovery.CURRENT_VALID:
                    self._consecutive_auth_failures += 1
                    print(f"ℹ️ Session still valid — job request rejected: {label_text}")
                    if self._consecutive_auth_failures >= 2:
                        self._stop_run = True
                else:
                    self._consecutive_auth_failures += 1
                    self._stop_run = True
            else:
                self._consecutive_auth_failures = 0

            stats["failed"] += 1
            print(f"⚠️ Failed: {label_text} | {exc}")
            if self._stop_run:
                print("🛑 Repeated or unrecoverable authentication failure — stopping apply requests.")
            return True

    def _apply_core(self, lead, stats: dict, label_text: str) -> bool:
        job = lead.job
        source = lead.source

        details = self.job_client.get_job_details(job.job_id)

        # Only the details call can catch externals the listing didn't flag.
        # Document and move on — never an apply attempt, never any budget.
        if self.job_client.is_external(details):
            self._document_external(job, source, stats, label_text)
            return False

        # Human "reads" the job description before hitting apply —
        # longer JDs take proportionally longer. Stripped of HTML first, so
        # the pause tracks the words and the role filter judges the words.
        jd_text = plain_text(
            (details.get("job") or {}).get("description") or job.description or ""
        )
        humanizer.reading_pause(len(jd_text))

        # Now that the real JD is in hand, confirm this is target AI/ML work.
        # Clear GenAI builder titles pass locally; ambiguous jobs use the model.
        # Keyword search still returns QA and unrelated engineering listings.
        # Costs no apply budget — it's the same as reading and walking away.
        verdict = self._role_verdict(job, jd_text)
        if verdict is False:
            stats["skipped_irrelevant"] += 1
            print(f"🧠 Outside hands-on AI/GenAI target: {label_text}")
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

        outcome = parse_apply_response(result, job.job_id)
        questionnaire_answers = []
        if outcome.status is ApplyStatus.QUESTIONNAIRE:
            # Humans take time to fill in a questionnaire.
            humanizer.human_delay(2.0, 8.0)
            sid = humanizer.generate_sid()
            questionnaire_result = self.job_client.handle_static_questionnaire_and_apply(
                job,
                questionnaire=outcome.questionnaire,
                sid=sid,
                mandatory_skills=mandatory,
                optional_skills=optional,
                source=source,
            )
            if isinstance(questionnaire_result, dict):
                questionnaire_answers = (
                    questionnaire_result.get("_questionnaire_answers") or []
                )
            outcome = parse_apply_response(
                questionnaire_result,
                job.job_id,
                final_submission=True,
            )

        if outcome.status is ApplyStatus.ALREADY_APPLIED:
            self._consecutive_auth_failures = 0
            self.applied_job_ids.add(outcome.job_id)
            stats["skipped_applied"] += 1
            print(f"⏭️ Server says already applied: {label_text}")
            return True

        if outcome.status is not ApplyStatus.APPLIED:
            raise RuntimeError(
                f"Apply not confirmed ({outcome.status.value}): {outcome.reason}"
            )

        save_applied_job(job, questionnaire_answers=questionnaire_answers)
        self._consecutive_auth_failures = 0
        self.applied_job_ids.add(outcome.job_id)
        stats["applied"] += 1

        print(f"✅ Applied: {label_text} ({age_label(job)})")
        return True
