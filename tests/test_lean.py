"""Self-check for the logic touched by the over-engineering cleanup.

Plain asserts, no test framework:  python tests/test_lean.py

Covers only what the cleanup actually changed shape of: the questionnaire
hint lists, the Counter-based run stats, the shuffled() stdlib swap, the
shared LLM fail-soft path, CSV header-on-first-write, the phase-scoped
block counters, transient-error retries, HTML-stripped job descriptions,
high-recall AI/GenAI role filtering, and the browse-only gate.
"""

import csv
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agents import easy_apply_agent
from src.agents.easy_apply_agent import EasyApplyAgent
from src.agents.job_store import write_csv_row
from src.agents.job_utils import empty_stats, plain_text
from src.client import job_client
from src.client.job_client import NaukriJobClient
from src.config import agent_config as config
from src.models.models import Job, JobLead
from src.utils import ai_role_filter, humanizer, llm
from src.utils.questionnaire import _answer_one

YES_NO = {"1": "Yes", "2": "No"}


def _answer(question, qtype="radio button", options=None):
    return _answer_one(
        {"questionId": "q", "questionName": question, "questionType": qtype,
         "answerOption": options if options is not None else YES_NO},
        config.QUESTIONNAIRE_PROFILE,
        [],
    )


def check_f2f_hints_still_force_no():
    """F2F_INTERVIEW_HINTS now lives only in one place, appended into
    NO_QUESTION_HINTS. Every in-person phrasing must still answer No."""
    for q in (
        "Are you available for a F2F interview?",
        "Can you attend a face to face interview?",
        "Are you open to a face-to-face round?",
        "Can you attend an in person interview?",
        "Are you available for an in-person discussion?",
        "Is inperson attendance possible?",
        "Are you available for a walk-in?",
        "Can you attend the walkin drive?",
    ):
        assert _answer(q) == ["2"], f"expected No for {q!r}, got {_answer(q)}"
        assert _answer(q, qtype="text box") == "No", q

    # The de-duplication must not have swallowed the non-F2F terms.
    for q in ("Have you worked here before?", "Have you applied to this company earlier?"):
        assert _answer(q) == ["2"], q

    # A plain (non-F2F) interview availability question still answers Yes.
    assert _answer("Are you available for a telephonic interview?") == ["1"]


def check_stats_counter():
    """Counter replaced STATS_KEYS + empty_stats + add_stats."""
    stats = empty_stats(found=7)
    assert stats["found"] == 7
    # Keys never incremented must read 0, since _print_summary reads them all.
    for key in ("attempted", "applied", "skipped_ext", "skipped_applied",
                "skipped_blocked", "skipped_excluded", "skipped_irrelevant",
                "skipped_browse", "external_written", "external_updated",
                "external_duplicate", "external_unsaved", "failed"):
        assert stats[key] == 0, key
    stats["applied"] += 1
    stats["applied"] += 1
    assert stats["applied"] == 2
    assert empty_stats()["found"] == 0


def check_shuffled_is_a_copy():
    """random.sample(x, len(x)) replaced the manual copy+shuffle."""
    original = list(range(50))
    out = humanizer.shuffled(original)
    assert original == list(range(50)), "original was mutated"
    assert sorted(out) == original, "elements lost or duplicated"
    assert len(out) == 50
    assert humanizer.shuffled([]) == []


def check_llm_fails_soft_and_trips():
    """llm.call must return None (never raise) and trip after AI_MAX_FAILURES."""
    name = "selfcheck-endpoint"
    llm._failures.pop(name, None)
    assert llm.tripped(name) is False

    for _ in range(config.AI_MAX_FAILURES):
        got = llm.call(
            name,
            "http://127.0.0.1:1/never-listening",
            {}, {"x": 1},
            lambda data: "unreachable",
        )
        assert got is None, got

    assert llm.tripped(name) is True, "trip switch never fired"
    llm._failures.pop(name, None)


def check_search_challenge_does_not_veto_apply():
    """A search-phase recaptcha must not abort the apply phase.

    register_challenge sets a process-global flag; without the phase reset
    too_many_blocks() stayed True forever and the apply loop broke on its
    first iteration (pool collected, 0 attempted).
    """
    humanizer.reset_blocks()
    assert humanizer.too_many_blocks() is False

    humanizer.PACER.challenged = True
    humanizer.PACER.blocks_seen = config.MAX_BLOCKS_BEFORE_ABORT
    assert humanizer.too_many_blocks() is True

    humanizer.reset_blocks()
    assert humanizer.too_many_blocks() is False, "apply phase still vetoed"

    # Apply-side pushback must still be able to abort from zero.
    humanizer.PACER.blocks_seen = config.MAX_BLOCKS_BEFORE_ABORT
    assert humanizer.too_many_blocks() is True
    humanizer.reset_blocks()


def check_retry_transient_survives_transport_errors():
    """A raised transport error ("Request failed") must retry, not burn the job."""
    class _Res:
        def __init__(self, code):
            self.status_code = code

    job_client.time.sleep = lambda _s: None  # skip the real backoff waits
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("Request failed")
        return _Res(200)

    assert NaukriJobClient._retry_transient(flaky, "test").status_code == 200
    assert len(calls) == 2, calls

    # Still gives up (and re-raises) once the attempts are spent.
    always = []

    def dead():
        always.append(1)
        raise RuntimeError("Request failed")

    try:
        NaukriJobClient._retry_transient(dead, "test")
    except RuntimeError:
        pass
    else:
        raise AssertionError("exhausted retries must re-raise")
    assert len(always) == config.TRANSIENT_RETRY_ATTEMPTS, always

    # 5xx still retries and returns the last response instead of raising.
    codes = [500, 500, 200]
    assert NaukriJobClient._retry_transient(
        lambda: _Res(codes.pop(0)), "test"
    ).status_code == 200

    job_client.time.sleep = time.sleep


def check_plain_text_strips_naukri_html():
    """Naukri JDs are HTML — the role filter and reading timer need words."""
    raw = (
        "<p><strong>Role:</strong>&nbsp;AI Engineer</p>"
        "<ul><li>Build RAG&nbsp;pipelines</li><li>Fine-tune LLMs &amp; agents</li></ul>"
        "<div>C++ &lt;algorithm&gt; a plus</div>"
    )
    out = plain_text(raw)

    for tag in ("<p>", "</p>", "<ul>", "<li>", "</li>", "<strong>", "<div>"):
        assert tag not in out, (tag, out)
    assert "&nbsp;" not in out and "&amp;" not in out, out
    assert "Fine-tune LLMs & agents" in out, out          # entities decoded
    assert "pipelinesFine" not in out, out                # bullets kept apart
    assert out.startswith("Role: AI Engineer"), out
    # Unescape runs AFTER tag-stripping, so an escaped angle bracket in the
    # JD text survives as text and can never be mistaken for a tag.
    assert "C++ <algorithm> a plus" in out, out
    assert plain_text("") == "" and plain_text(None) == ""


def check_jd_is_not_truncated_by_default():
    """AI_ROLE_FILTER_JD_CHARS = 0 must mean 'whole JD', not 'empty JD'."""
    assert config.AI_ROLE_FILTER_JD_CHARS == 0
    jd = "x" * 5000
    sent = jd if config.AI_ROLE_FILTER_JD_CHARS <= 0 else jd[:config.AI_ROLE_FILTER_JD_CHARS]
    assert len(sent) == 5000, len(sent)
    # The prompt must not tell the model the description may be truncated,
    # or it will fall back to judging the title.
    assert "may be truncated" not in ai_role_filter.SYSTEM_PROMPT


def check_genai_title_high_recall_gate():
    """Clear GenAI builders survive generic JDs; non-target titles do not."""
    positives = (
        "Agentic AI Engineer",
        "GenAI & Agentic AI Engineer",
        "Senior Python / Conversational AI Engineer",
        "Salesforce GenAI Engineer",       # "sales" must not match Salesforce
        "LLM Developer",
        "LLM Ops Engineer",
        "LLM Operations Engineer",
        "LLM Training Engineer",
        "Agentic AI Engineers",
        "GenAI Developers",
        "LLM Architects",
        "GenAI Data Scientists",
        "RAG Architect",
        "GenAI Technical Lead",
        "Gen AI",
    )
    for title in positives:
        assert ai_role_filter._title_confirms_genai(title), title

    not_overrides = (
        "GenAI QA Engineer",
        "Agentic AI Sales Engineer",
        "Presales GenAI Engineer",
        "LLM Testing Engineer",
        "GenAI Product Owner",
        "RAG Business Analyst",
        "LLM Data Annotator",
        "LLM Account Executive",
        "GenAI Director of Engineering",
        "Generative AI Consultant",        # ambiguous: model reads the JD
        "AI Governance",
        "AI Test Engineer",
        "Lead Java AI Engineer",           # broad AI: model reads the JD
        "Computer Vision Engineer",        # broad AI: model reads the JD
        "Storage Engineer",                # contains letters 'rag', not token
        "Pragmatic Software Engineer",      # contains letters 'rag', not token
    )
    for title in not_overrides:
        assert not ai_role_filter._title_confirms_genai(title), title

    assert ai_role_filter._title_confirms_genai(
        "Agentic AI Engineer",
        "Generic software development and team collaboration.",
    )
    assert not ai_role_filter._title_confirms_genai(
        "Agentic AI Engineer",
        "Own presales, marketing, and business development.",
    )
    for contradiction in (
        "Own test automation and regression testing.",
        "Define AI risk policy and ethics controls.",
        "Lead partnerships, account management, and revenue operations.",
    ):
        assert not ai_role_filter._title_confirms_genai(
            "Agentic AI Engineer", contradiction,
        ), contradiction

    assert ai_role_filter._is_ordinary_qa_role(
        "AI Test Engineer",
        "Automation and performance testing with JMeter and Gatling.",
    )
    assert not ai_role_filter._is_ordinary_qa_role(
        "LLM Testing Engineer",
        "Own LLM evaluation, red teaming, guardrails, and model quality.",
    )
    assert not ai_role_filter._is_ordinary_qa_role(
        "LLM Testing Engineer",
        "Design automated test cases and evaluate LLM outputs for "
        "hallucinations, factuality, and robustness.",
    )

    saved_api_key = llm.api_key
    try:
        llm.api_key = lambda name: {
            "OPENAI_API_KEY": "",
            "OPEN_API_KEY": "legacy-key",
        }.get(name, "")
        assert ai_role_filter._openai_api_key() == "legacy-key"
    finally:
        llm.api_key = saved_api_key


def check_ai_role_filter_semantic_contract():
    """Protect broad AI scope, title fallback, and exact verdict parsing."""
    for required in (
        "machine learning",
        "computer vision",
        "MLOps/LLMOps",
        "Programming language is not a gate",
        "explicit AI/ML/GenAI title is strong evidence",
    ):
        assert required in ai_role_filter.SYSTEM_PROMPT, required

    saved_enabled = config.AI_ROLE_FILTER
    saved_health = ai_role_filter.role_filter_enabled
    saved_ask = ai_role_filter._ask_openai
    calls = []
    config.AI_ROLE_FILTER = True
    ai_role_filter.role_filter_enabled = lambda: True
    ai_role_filter._verdict_cache.clear()

    try:
        def should_not_call(_prompt):
            raise AssertionError("clear GenAI title unexpectedly called model")

        ai_role_filter._ask_openai = should_not_call
        assert ai_role_filter.is_relevant_role(
            "Agentic AI Engineer",
            description="Generic software development and team collaboration.",
        ) is True
        assert ai_role_filter.is_relevant_role(
            "AI Test Engineer",
            description="Automation and performance testing with JMeter and Gatling.",
        ) is False

        responses = iter(("NO", "`YES`", "NO", "not enough information"))

        def fake_model(prompt):
            calls.append(prompt)
            return next(responses)

        ai_role_filter._ask_openai = fake_model
        assert ai_role_filter.is_relevant_role(
            "Agentic AI Engineer",
            description="Own presales, marketing, and business development.",
        ) is False
        assert ai_role_filter.is_relevant_role(
            "Computer Vision Engineer",
            description="Train and deploy deep-learning vision models.",
        ) is True
        assert ai_role_filter.is_relevant_role(
            "AI Governance",
            description="Own AI policy, compliance, audit, and risk controls.",
        ) is False
        assert ai_role_filter.is_relevant_role(
            "Backend Engineer",
            description="Build REST APIs.",
        ) is None
        assert len(calls) == 4, calls
    finally:
        config.AI_ROLE_FILTER = saved_enabled
        ai_role_filter.role_filter_enabled = saved_health
        ai_role_filter._ask_openai = saved_ask
        ai_role_filter._verdict_cache.clear()


def check_confirmed_ai_role_is_never_browsed_away():
    """A confirmed AI role must apply even when browse-only is certain to fire.

    Drives the real _apply_core with window shopping forced on, so this
    proves behaviour rather than asserting on source text.
    """
    job = Job(job_id="1", title="Agentic AI Engineer", company="Infosys",
              location="Pune", experience="5", salary="x", posted_date="today",
              apply_link="x", description="Build agentic AI systems.")
    lead = JobLead(job=job, source="search")

    class _Client:
        applied = 0

        def get_job_details(self, job_id, sid=""):
            return {"job": {"description": "Build LLM agents and RAG pipelines."}}

        @staticmethod
        def is_external(details):
            return False

        def apply_job(self, job, **kw):
            _Client.applied += 1
            return {"success": True, "jobs": [{"jobId": job.job_id}]}

    def _run(verdict):
        _Client.applied = 0
        agent = EasyApplyAgent.__new__(EasyApplyAgent)      # skip CSV loading
        agent.job_client = _Client()
        agent.external_link_agent = None
        agent.applied_job_ids = set()
        agent._role_verdict = staticmethod(lambda *a, **k: verdict)
        stats = empty_stats()
        agent._apply_core(lead, stats, "test")
        return stats

    saved_prob = config.WINDOW_SHOPPING_PROBABILITY
    saved_save = easy_apply_agent.save_applied_job
    saved_pause = humanizer.reading_pause
    config.WINDOW_SHOPPING_PROBABILITY = 1.0               # browse ALWAYS fires
    easy_apply_agent.save_applied_job = lambda *a, **k: None
    humanizer.reading_pause = lambda *a, **k: None
    try:
        assert humanizer.window_shopping() is True, "probability not honoured"

        ai = _run(True)
        assert ai["applied"] == 1 and ai["skipped_browse"] == 0, dict(ai)
        assert _Client.applied == 1

        not_ai = _run(False)
        assert not_ai["skipped_irrelevant"] == 1 and not_ai["applied"] == 0, dict(not_ai)

        # No verdict (filter off/unkeyed/tripped) is the only browse-only case.
        unknown = _run(None)
        assert unknown["skipped_browse"] == 1 and unknown["applied"] == 0, dict(unknown)
    finally:
        config.WINDOW_SHOPPING_PROBABILITY = saved_prob
        easy_apply_agent.save_applied_job = saved_save
        humanizer.reading_pause = saved_pause


def check_csv_header_written_once():
    """ensure_csv_fieldnames is gone; write_csv_row still headers a new file."""
    fields = ["job_id", "title"]
    path = os.path.join(tempfile.mkdtemp(), "rows.csv")

    write_csv_row(path, fields, {"job_id": "1", "title": "a"})
    write_csv_row(path, fields, {"job_id": "2", "title": "b"})

    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert [r["job_id"] for r in rows] == ["1", "2"], rows
    assert open(path, encoding="utf-8").read().count("job_id,title") == 1


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    for check in (
        check_f2f_hints_still_force_no,
        check_stats_counter,
        check_shuffled_is_a_copy,
        check_llm_fails_soft_and_trips,
        check_search_challenge_does_not_veto_apply,
        check_retry_transient_survives_transport_errors,
        check_plain_text_strips_naukri_html,
        check_jd_is_not_truncated_by_default,
        check_genai_title_high_recall_gate,
        check_ai_role_filter_semantic_contract,
        check_confirmed_ai_role_is_never_browsed_away,
        check_csv_header_written_once,
    ):
        check()
        print(f"ok  {check.__name__}")
    print("\nall checks passed")
