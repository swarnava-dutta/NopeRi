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
    """Answer one question with the AI stubbed out.

    Answering is AI-first in production, but these checks cover the rule
    layer: the strategy rules that must win over the model, and the offline
    cascade that runs when it is unavailable. Letting a real API call happen
    here would make the suite slow, network-dependent, and non-deterministic
    — a live model returning a different-but-valid answer is not a
    regression, so it must not be able to fail the build.
    """
    import src.utils.questionnaire as qn

    saved_text, saved_option = qn.ai_text_answer, qn.ai_option_answer
    qn.ai_text_answer = lambda _q: None
    qn.ai_option_answer = lambda *_a, **_k: None
    try:
        return _answer_one(
            {"questionId": "q", "questionName": question, "questionType": qtype,
             "answerOption": options if options is not None else YES_NO},
            config.QUESTIONNAIRE_PROFILE,
            [],
        )
    finally:
        qn.ai_text_answer, qn.ai_option_answer = saved_text, saved_option


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


def check_profile_answers_every_free_text_type():
    """Profile rules must run for EVERY typed question, not just "Text Box".

    Naukri also sends "date", "Text Area", and option-less "List Menu"; those
    used to bypass all the fixed rules and reach the LLM, which then invented
    a date of birth or a PAN instead of reading candidate_profile.json.
    """
    profile = config.QUESTIONNAIRE_PROFILE
    cases = (
        ("Date of Birth", "date_of_birth"),
        ("PAN Number", "pan_number"),
        ("LinkedIn Profile", "linkedin_url"),
        ("Mobile Number", "phone"),
        ("Current Location", "current_location"),
        ("Current Company", "current_company"),
        ("Email ID", "email"),
        ("Mention the email which is registered with TCS",
         "tcs_registration_email"),
        ("Share your TCS EP number", "tcs_ep_number"),
        ("Full Name", "full_name"),
    )
    for qtype in ("text box", "date", "text area", "list menu", ""):
        for question, key in cases:
            got = _answer(question, qtype=qtype, options={})
            assert got == profile[key], (qtype, question, got)

    # full_name is split for the first/last variants recruiters actually ask.
    first, last = profile["full_name"].split()[0], profile["full_name"].split()[-1]
    assert _answer("First Name", qtype="text box", options={}) == first
    assert _answer("Last Name", qtype="text box", options={}) == last
    # ...but a company/college name is not the candidate's name.
    assert _answer("Current Company Name", qtype="text box", options={}) == \
        profile["current_company"]


def check_evasive_ai_answers_are_discarded():
    """A hedge/deferral is not an answer — it must never reach the form.

    The detector matches the SHAPE of an evasion, not remembered sentences:
      1. negated possession/provision  "I don't have it"
      2. provision promised for later  "I'll share it"
      3. provision tied to a process   "upon request during onboarding"

    So the fixtures below are deliberately split. The first group is real
    output captured from logs/noperi_hidden.log; the second is held-out
    rewordings that appear nowhere in the patterns, which is what proves the
    rule generalizes instead of memorizing. The old detector needed an
    "unavailable" phrase AND a source word ("profile"), so every answer in
    the first group was submitted verbatim.
    """
    from src.utils.ai_answer import _mentions_missing_profile_data as evasive

    observed = (
        "I don't have an employee code to share as I'm not currently "
        "employed at the organization being asked about.",
        "I don't have my PAN number readily available, but I can provide it "
        "upon request during the formal onboarding process.",
        "I don't have my PAN number readily available in the provided "
        "information. I'll furnish it during the document upload process.",
        "I can provide my last working day date upon finalization of the offer.",
        "I don't have specific data on monthly token consumption.",
        "I don't have my CIBIL score screenshot readily available, but I can "
        "provide it upon request during the verification process.",
        "I don't have a specific LinkedIn profile URL to provide, but I'm "
        "active on LinkedIn and can be reached there upon request.",
    )
    held_out = (
        "That information is not something I can disclose at this point.",
        "Regrettably I am unable to furnish those particulars right now.",
        "My Aadhaar is not with me at the moment.",
        "I would be glad to supply the certificate once shortlisted.",
        "The reference number can be produced on demand.",
        "I shall send the payslips prior to joining.",
        "I cannot recall the exact figure offhand.",
        "Happy to disclose this at the appropriate stage.",
        "Details will be submitted during the background check.",
        "This will be confirmed closer to the offer.",
        "I lack the paperwork needed to answer this.",
        # Denies the ANSWER rather than the possession of a document.
        "I have no data on token burn metrics from my work history.",
        "This question is not applicable to my professional profile.",
        "I have no record of that.",
    )
    for bad in observed + held_out:
        assert evasive(bad) is True, bad

    # Genuine answers must survive — including ones that merely LOOK evasive
    # because they contain "no", "not", "during", "after", or an offer to do
    # something ("share my screen") rather than to hand a value over later.
    for good in (
        "N/A", "n/a", "No", "None", "Yes", "6", "9 September 1997",
        "CSNPD8764M", "Kolkata", "Not applicable", "No experience",
        "I have built RAG pipelines with LangChain and LangGraph.",
        "I have no objection to relocating to Hyderabad or Bangalore.",
        "I can share my screen during the technical round.",
        "I have all the required documents ready.",
        "I have provided support to five enterprise clients.",
        "I can start immediately after 1 September 2026.",
        "I have no gaps in my employment history.",
        "During my tenure at TEKsystems I built agentic AI systems.",
        "I can join within 30 days.",
        # Positive "no ..." statements: the negation is the good news here,
        # so they must not be confused with a denial of the answer itself.
        "I have no issues with night shifts.",
        "There are no blockers on my end.",
        # Confident quantities produced by the committed-answer rule.
        "1500000",
        "10 million records across multiple enterprise RAG projects.",
    ):
        assert evasive(good) is False, good


def check_unanswerable_text_falls_back_to_na():
    """With the AI off, a fallback must never make the candidate look worse.

    A blank-ish answer is not neutral: "N/A" on "can you share X" reads as a
    refusal, and "1" on a volume question reads as no production experience.
    """
    # No sensible value exists → clean non-answer.
    assert _answer("Employee Code", qtype="text box", options={}) == "N/A"
    assert _answer("Java Version:", qtype="text box", options={}) == "N/A"

    # Willingness to supply something is always Yes — never an apology.
    for q in (
        "Can you share the Screenshot of your CIBIL score",
        "Can you provide your last 3 months payslips?",
        "Are you willing to share your Aadhaar card?",
        "Would you be able to submit your relieving letter?",
        "Can you bring the original documents?",
    ):
        assert _answer(q, qtype="text box", options={}) == "Yes", q

    # ...but when the profile HAS the value, the value beats "Yes".
    profile = config.QUESTIONNAIRE_PROFILE
    assert _answer("Can you share your PAN number?", qtype="text box",
                   options={}) == profile["pan_number"]
    assert _answer("Could you provide your LinkedIn profile?",
                   qtype="text box", options={}) == profile["linkedin_url"]

    # Inverted polarity ("any objection to...") must not answer "Yes".
    assert _answer("Any objection to sharing your reference details?",
                   qtype="text box", options={}) != "Yes"

    # Experience questions still resolve from the profile.
    assert _answer(
        "How many years of Kubernetes?", qtype="text box", options={}
    ) == profile["exp_total"]

    # Volume questions get a credible production figure, not "1".
    for q in ("How many tokens have you burnt in a month?",
              "How many users did your system serve?",
              "Number of documents processed per day?"):
        got = _answer(q, qtype="text box", options={})
        assert got == config.SCALE_TEXTBOX_ANSWER, (q, got)
        assert int(got) > 1000, got


def check_profile_context_sends_every_field():
    """Answering is AI-first, so the model must see the WHOLE profile.

    Each field used to need a hand-written line in _profile_context AND a
    matching keyword rule, so a key added to candidate_profile.json was
    invisible until both were edited. Serializing the dict means a new field
    works on the next run with no code change.
    """
    from src.utils.ai_answer import _profile_context

    context = _profile_context()
    profile = config.QUESTIONNAIRE_PROFILE

    for key, value in profile.items():
        if not value:
            continue                      # empty fields are skipped as noise
        if isinstance(value, (list, tuple)):
            needle = str(value[0])
        else:
            needle = str(value)
        assert needle in context, (key, needle)

    # An unknown key still reaches the model, labelled from its own name.
    profile["favourite_editor"] = "Neovim"
    try:
        refreshed = _profile_context()
        assert "favourite editor: Neovim" in refreshed, refreshed
    finally:
        profile.pop("favourite_editor")


def check_ctc_breakup_is_not_invented():
    """The salary breakup must come from the profile, never from a guess.

    Asked for a fixed/variable split the model assumed a typical 80/20 one
    ("Fixed: 32 lakhs, Variable: 8.5 lakhs") when the whole CTC is fixed. A
    breakup that contradicts the payslip is caught at verification, so the
    split has to be stated in candidate_profile.json and used verbatim.
    """
    profile = config.QUESTIONNAIRE_PROFILE

    # The split is data, not something the prompt or a rule may infer.
    assert profile["current_ctc_fixed"] == profile["current_ctc"], profile
    assert profile["current_ctc_variable"] == "0", profile

    from src.utils.ai_answer import _profile_context

    context = _profile_context()
    assert "Current FIXED CTC" in context, context
    assert "Current VARIABLE CTC" in context, context
    # The zero must survive the empty-value filter: "0" is a real answer,
    # and dropping it would let the model assume a variable component again.
    variable_line = [l for l in context.splitlines() if "VARIABLE CTC" in l]
    assert variable_line and "0" in variable_line[0], context


def check_ctc_units_and_dynamic_notice():
    """Unitless text stays readable; restricted inputs stay numeric-only."""
    from datetime import date

    from src.config.agent_config import _remaining_notice_days

    assert _remaining_notice_days(
        "28 August 2026", date(2026, 8, 12)
    ) == 16
    assert _remaining_notice_days(
        "1 August 2026", date(2026, 8, 12)
    ) == 0
    assert _remaining_notice_days("not a date", date(2026, 8, 12)) is None

    profile = config.QUESTIONNAIRE_PROFILE
    saved_notice = profile["notice_days"]
    profile["notice_days"] = 16
    try:
        assert _answer("Current CTC:", qtype="text box", options={}) == "40.5 LPA"
        assert _answer("Expected CTC:", qtype="text box", options={}) == "55 LPA"
        assert _answer("Notice period:", qtype="text box", options={}) == "16 days"

        # Unit already stated: do not repeat it. Explicit numeric-only CTC
        # fields retain raw annual INR, as required by Naukri's API.
        assert _answer("Current CTC in LPA", qtype="text box", options={}) == "40.5"
        assert _answer("Expected CTC in lakhs", qtype="text box", options={}) == "55"
        assert _answer("Notice period in days", qtype="text box", options={}) == "16"
        assert _answer(
            "Current CTC (Numeric Input Only)", qtype="text box", options={}
        ) == "4050000"

        # Custom forms sometimes send notice options in descending order.
        # Selection must use duration, not whichever qualifying label appears first.
        descending_days = {"90": "90", "60": "60", "30": "30", "15": "15"}
        assert _answer(
            "Notice Period (In Days)", qtype="list menu",
            options=descending_days,
        ) == ["30"]
        status_options = {
            "not": "Not Serving Notice Period",
            "serving": "Serving Notice Period",
        }
        assert _answer(
            "What is your notice period?", qtype="list menu",
            options=status_options,
        ) == ["serving"]

        lwd_answer = _answer(
            "What is your notice period? Please mention LWD if serving notice",
            qtype="text box", options={},
        )
        assert lwd_answer == "16 days; LWD: 28 August 2026", lwd_answer
    finally:
        profile["notice_days"] = saved_notice


def check_headcount_comes_from_profile():
    """"How many people have you mentored?" is a headcount, not a duration.

    It contains "how many", so without its own rule it fell through to the
    years rule (answering the experience years) or to the LLM, which invented
    a different number on every application.
    """
    profile = config.QUESTIONNAIRE_PROFILE
    for q in (
        "How many people have you mentored?",
        "How many engineers have you led?",
        "What is the size of the team you mentored?",
        "Number of developers you have managed?",
    ):
        got = _answer(q, qtype="text box", options={})
        assert got == profile["team_mentored"], (q, got)

    # A years question that also mentions leading must stay a years answer.
    assert _answer("How many years have you led AI projects?",
                   qtype="text box", options={}) == profile["exp_ai"]


def check_long_answers_are_not_cut_mid_word():
    """A visibly truncated sentence is a worse tell than a short answer.

    The old hard slice at 500 chars produced "...RAGAS for retrieval quality
    me", which reads as broken automation on a real application.
    """
    from src.utils.ai_answer import _clean_text_answer

    # Short answers pass through untouched.
    assert _clean_text_answer("  9  ") == "9"
    assert _clean_text_answer('"Kolkata"') == "Kolkata"

    # Multi-sentence overflow trims back to the last complete sentence.
    long_answer = ("I built a multi-agent RAG system using LangGraph. " * 12)
    out = _clean_text_answer(long_answer)
    assert len(out) <= 500, len(out)
    assert out.endswith("."), out
    assert not out.endswith(" ."), out
    assert "LangGraph" in out

    # A single unbroken sentence still ends on a whole word, not mid-token.
    unbroken = "word " * 200
    out2 = _clean_text_answer(unbroken)
    assert len(out2) <= 500, len(out2)
    assert not out2.rstrip(".").endswith("wor"), out2


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
        check_profile_answers_every_free_text_type,
        check_evasive_ai_answers_are_discarded,
        check_unanswerable_text_falls_back_to_na,
        check_profile_context_sends_every_field,
        check_ctc_breakup_is_not_invented,
        check_ctc_units_and_dynamic_notice,
        check_headcount_comes_from_profile,
        check_long_answers_are_not_cut_mid_word,
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
