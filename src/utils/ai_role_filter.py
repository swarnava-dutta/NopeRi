"""GPT-powered check: is a listing genuinely an AI/ML engineering role?

Naukri's keyword search matches the whole listing, not the title, so searching
"Applied AI Engineer" also returns QA testers, C# developers, and even
"CA Intermediate | Team Leader". A keyword blocklist can't keep up with that
variety, so the model judges each job instead.

The verdict rests on the DESCRIPTION, not the title: titles are written to
attract applicants ("AI Engineer" over a Java backend JD), so the whole
HTML-stripped description is sent and the title is only a fallback when there
is no description at all.

Uses OpenAI's synchronous chat completions (``ROLE_FILTER_MODEL``, default
``gpt-5.6-luna``). The Batch API is deliberately NOT used: it is asynchronous
with up to a 24h completion window (measured: still "validating" after 30s,
vs ~1.6s for a sync call), and this verdict is needed inline, mid-apply, to
decide whether to submit an application right now.

Requires ``OPENAI_API_KEY`` in the environment / ``.env`` file.
Fails soft: returns ``None`` on any problem so callers keep their own
behaviour instead of silently dropping jobs. After ``AI_MAX_FAILURES``
consecutive failures it disables itself for the rest of the run — but a
truncated (empty) reply is NOT such a failure, since the endpoint answered
fine; see ``_first_choice``.
"""

import logging

from src.config import agent_config as config
from src.utils import llm

logger = logging.getLogger(__name__)

OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
CALLER = "OpenAI role filter"

# Verdicts are cached per run, keyed by the full prompt: overlapping search
# terms return the identical listing repeatedly, and an identical prompt can
# only have an identical verdict.
_verdict_cache: dict[str, bool] = {}

SYSTEM_PROMPT = (
    "You screen job listings for an AI/ML engineer. Decide whether a listing "
    "is genuinely an AI/ML engineering role.\n\n"
    "Answer YES only if the CORE of the job is building AI/ML systems: "
    "machine learning, deep learning, generative AI, LLMs, RAG, agentic AI, "
    "NLP, computer vision, MLOps, data science with modelling, or an AI "
    "platform / AI product engineering role. A software engineering role "
    "whose main purpose is building AI features also counts.\n\n"
    "Answer NO for everything else, including:\n"
    "- General software engineering with no AI focus (frontend, backend, "
    "full-stack, .NET/C#, Java, SAP, Salesforce, embedded, DevOps, cloud "
    "admin) where AI is absent or only mentioned in passing as a buzzword\n"
    "- QA / testing / automation testing roles\n"
    "- Data engineering / ETL / warehousing with no modelling\n"
    "- Data analyst / BI / reporting roles\n"
    "- Non-engineering roles: sales, support, HR, finance, accounting, "
    "operations, teaching, content, recruitment, team leader/manager roles "
    "that are not hands-on AI engineering\n"
    "- Roles requiring a completely different profession (e.g. chartered "
    "accountant, doctor, lawyer, mechanical/civil engineer)\n\n"
    "Judge the actual work described, not marketing words. Many listings "
    "sprinkle 'AI' into an unrelated job — those are NO.\n\n"
    "Important:\n"
    "- THE DESCRIPTION DECIDES. Titles are written to attract applicants; "
    "the responsibilities and required skills are what the job actually is. "
    "A listing titled 'AI Engineer' or 'Data Scientist' whose description is "
    "really Java/.NET backend work, QA, support, BI reporting, staffing, or "
    "plain ETL is NO — the title does not rescue it.\n"
    "- The reverse also holds: a plainly-titled 'Software Engineer' whose "
    "description is building LLM/ML systems is YES.\n"
    "- Fall back to the title ONLY when the description is empty or says "
    "nothing about the work.\n"
    "- When the description genuinely describes AI work but is ambiguous "
    "about how central it is, answer YES. Missing a real AI job is worse "
    "than sending one extra application.\n\n"
    "Reply with exactly one word: YES or NO."
)


def role_filter_enabled() -> bool:
    """True when the role filter is switched on, keyed, and still healthy."""
    if not config.AI_ROLE_FILTER:
        return False
    if llm.tripped(CALLER):
        return False
    return bool(llm.api_key("OPENAI_API_KEY"))


def _first_choice(data: dict) -> str:
    """Content of the first chat-completions choice.

    Raises ``llm.NoFault`` on an empty reply: the API answered fine, so this
    must not count toward the consecutive-failure breaker (see below).
    """
    choice = (data.get("choices") or [{}])[0]
    text = ((choice.get("message") or {}).get("content") or "").strip()
    if not text:
        # Empty content with finish_reason="length" means the token cap was
        # eaten by internal reasoning before any visible token was written.
        # That's a truncated answer from a HEALTHY endpoint, so it is raised
        # as NoFault rather than returned as "" — counting it as a failure
        # let three long JDs in a row trip AI_MAX_FAILURES and disable the
        # filter for the whole run, after which every job silently fell
        # through to the window-shopping fallback.
        reason = choice.get("finish_reason")
        usage = (data.get("usage") or {}).get("completion_tokens_details") or {}
        raise llm.NoFault(
            f"empty reply (finish_reason={reason}, "
            f"reasoning_tokens={usage.get('reasoning_tokens', '?')}, "
            f"cap={config.ROLE_FILTER_MAX_TOKENS})"
        )
    return text


def _ask_openai(user_prompt: str) -> str | None:
    """One sync chat completion. Returns the reply text, or None on failure."""
    # NOTE on parameters (verified against the live API for gpt-5.6-luna):
    #   * "max_tokens" is REJECTED — this model family requires
    #     "max_completion_tokens".
    #   * "temperature" only accepts its default (1); sending 0 is an error,
    #     so determinism has to come from the prompt, not the sampler.
    #   * "reasoning_effort" must be one of "none"/"low"/"medium"/"high"/
    #     "xhigh" — "minimal" is REJECTED by this model with HTTP 400
    #     ("does not support 'minimal' with this model"), which failed every
    #     single call and left the filter permanently silent. The level lives
    #     in config.ROLE_FILTER_REASONING_EFFORT ("medium"), and it is paired
    #     with ROLE_FILTER_MAX_TOKENS: reasoning tokens come out of the same
    #     cap and are spent before any visible token, so the two must be
    #     raised together (see _first_choice).
    return llm.call(
        CALLER,
        OPENAI_API_URL,
        {
            "Authorization": f"Bearer {llm.api_key('OPENAI_API_KEY')}",
            "Content-Type": "application/json",
        },
        {
            "model": config.ROLE_FILTER_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            # Generous vs the 1 token actually needed: reasoning-capable models
            # can spend this budget on internal tokens first, and a too-small
            # cap comes back as empty content with finish_reason="length".
            # Only tokens actually generated are billed, so the headroom is
            # free on the obvious listings and used only on the hard ones.
            "max_completion_tokens": config.ROLE_FILTER_MAX_TOKENS,
            "reasoning_effort": config.ROLE_FILTER_REASONING_EFFORT,
        },
        _first_choice,
        # Own timeout, not the shared AI_TIMEOUT_SECONDS: "medium" reasoning
        # thinks for many seconds on a long JD, and a timeout DOES count
        # against the AI_MAX_FAILURES breaker — three slow listings in a row
        # would disable the filter for the whole run.
        timeout=config.ROLE_FILTER_TIMEOUT_SECONDS,
    )


def is_relevant_role(title: str, company: str = "", tags=None,
                     description: str = "") -> bool | None:
    """Is this listing genuinely an AI/ML engineering role?

    Returns True/False from the model, or None when the filter is disabled,
    unkeyed, unhealthy, or the reply is unusable — callers MUST treat None as
    "don't know" and fall back to their own rules rather than dropping a job.
    """
    if not role_filter_enabled() or not (title or "").strip():
        return None

    tag_text = ", ".join(tags or [])[:300]
    # Whole description by default (AI_ROLE_FILTER_JD_CHARS = 0). The verdict
    # is supposed to rest on the described work, so truncating it would put
    # the decision back on the title — exactly what this filter exists to
    # avoid. Callers pass HTML-stripped text (job_utils.plain_text).
    jd = (description or "").strip()
    if config.AI_ROLE_FILTER_JD_CHARS > 0:
        jd = jd[: config.AI_ROLE_FILTER_JD_CHARS]

    prompt = (
        f"Job title: {title.strip()}\n"
        f"Company: {(company or 'N/A').strip()}\n"
        f"Skills/tags: {tag_text or 'N/A'}\n"
        f"Description: {jd or 'N/A'}\n\n"
        "Is this genuinely an AI/ML engineering role? Reply YES or NO."
    )

    # Keyed on the whole prompt, not title+company. Two postings can share a
    # title and company and still describe completely different work — reusing
    # the first verdict for both would defeat reading the description at all.
    if prompt in _verdict_cache:
        return _verdict_cache[prompt]

    raw = _ask_openai(prompt)
    if not raw:
        return None

    # Tolerant of "YES.", "**NO**", or a short sentence that starts with one.
    reply = " ".join(raw.split()).lower().strip("*.!,:\"' ")
    if reply.startswith("yes"):
        verdict = True
    elif reply.startswith("no"):
        verdict = False
    else:
        logger.warning("Unusable role-filter reply %r for %r", raw[:80], title)
        return None

    _verdict_cache[prompt] = verdict
    return verdict
