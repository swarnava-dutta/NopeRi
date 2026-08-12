"""GPT-powered check: is a listing hands-on AI/ML/GenAI engineering work?

Naukri's keyword search matches the whole listing, not the title, so searching
"Applied AI Engineer" also returns QA testers, C# developers, and even
"CA Intermediate | Team Leader". A keyword blocklist can't keep up with that
variety, so the model judges each job instead.

The title, skills, and whole HTML-stripped description are judged together.
Clear GenAI builder titles get a deterministic high-recall pass so generic
recruiter boilerplate cannot hide a real Agentic AI / LLM / RAG role. Other
titles are judged by the model from the complete listing.

Uses OpenAI's synchronous chat completions (``ROLE_FILTER_MODEL``, default
``gpt-5.6-luna``). The Batch API is deliberately NOT used: it is asynchronous
with up to a 24h completion window (measured: still "validating" after 30s,
vs ~1.6s for a sync call), and this verdict is needed inline, mid-apply, to
decide whether to submit an application right now.

Ambiguous titles require ``OPENAI_API_KEY`` in the environment / ``.env``.
The model path fails soft: it returns ``None`` on any problem so callers keep
their own behaviour instead of silently dropping jobs. After
``AI_MAX_FAILURES`` consecutive failures it disables itself for the rest of
the run — but a truncated (empty) reply is NOT such a failure, since the
endpoint answered fine; see ``_first_choice``.
"""

import logging
import re

from src.config import agent_config as config
from src.utils import llm

logger = logging.getLogger(__name__)

OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
CALLER = "OpenAI role filter"

# Verdicts are cached per run, keyed by the full prompt: overlapping search
# terms return the identical listing repeatedly, and an identical prompt can
# only have an identical verdict.
_verdict_cache: dict[str, bool] = {}

# A clear GenAI builder title is stronger evidence than a vague, templated JD.
# Keep this deliberately narrow: broad titles such as "AI Engineer", "MLOps
# Engineer", or "Data Scientist" still go through whole-listing judgement.
_GENAI_TITLE_RE = re.compile(
    r"\b(?:gen\s*ai|generative\s+(?:ai|artificial\s+intelligence)|"
    r"agentic\s+ai|ai\s+agents?|llm(?:\s*ops)?s?|large\s+language\s+models?|"
    r"rag|retrieval\s+augmented\s+generation|conversational\s+ai|"
    r"voice\s+ai)\b"
)
_BUILDER_TITLE_RE = re.compile(
    r"\b(?:engineers?|engineering|developers?|architects?|scientists?|"
    r"technical\s+leads?)\b"
)
_NON_TARGET_TITLE_RE = re.compile(
    r"\b(?:qa|quality\s+assurance|sdet|(?:test|testing)\s+engineer|governance|"
    r"responsible\s+ai|risk|compliance|audit|policy|ethics|sales|presales|"
    r"pre\s+sales|gtm|go\s+to\s+market|business\s+development|marketing|"
    r"demand\s+generation|account\s+executive|customer\s+success|support|"
    r"recruit(?:er|ment)?|trainer|teacher|writer|product\s+(?:manager|owner)|"
    r"business\s+analyst|data\s+analyst|"
    r"(?:business|revenue|sales|people)\s+operations|engineering\s+manager|"
    r"program\s+manager|project\s+manager|director|head|vp|vice\s+president)\b"
)
_NON_TARGET_JD_RE = re.compile(
    r"\b(?:manual\s+testing|automation\s+testing|test\s+automation|"
    r"automated\s+testing|regression\s+testing|quality\s+assurance|"
    r"test\s+cases?|governance|audit|compliance|risk|policy|ethics|"
    r"presales|pre\s+sales|sales|partnerships?|account\s+management|"
    r"revenue\s+operations|"
    r"go\s+to\s+market|business\s+development|marketing|customer\s+support|"
    r"recruit(?:ing|ment)|data\s+(?:annotation|labeling)|content\s+moderation)\b"
)
_QA_TITLE_RE = re.compile(
    r"\b(?:qa|quality\s+assurance|sdet|(?:test|testing)\s+engineer)\b"
)
_ORDINARY_QA_JD_RE = re.compile(
    r"\b(?:manual\s+testing|automation\s+testing|test\s+automation|"
    r"automated\s+testing|regression\s+testing|performance\s+testing|"
    r"test\s+cases?|selenium|jmeter|gatling|cypress|playwright|appium)\b"
)
_AI_EVALUATION_JD_RE = re.compile(
    r"\b(?:ai|ml|llm|model)\s+(?:evaluation|evals?|testing|quality)|"
    r"\b(?:evaluate|evaluating|benchmark|benchmarking|test|testing)\s+"
    r"(?:ai|ml|llms?|models?)(?:\s+outputs?)?|"
    r"\b(?:red\s+teaming|guardrails?|ai\s+safety|model\s+quality|"
    r"hallucinations?|factuality|toxicity|robustness|groundedness|faithfulness|"
    r"prompt\s+injection|bias\s+detection)\b"
)
_DOMAIN_ONLY_TITLES = {
    "gen ai",
    "generative ai",
    "generative artificial intelligence",
    "agentic ai",
    "llm",
    "llms",
    "llmops",
    "llm ops",
    "rag",
    "conversational ai",
    "voice ai",
}


def _normalized_title(title: str) -> str:
    """Lowercase title words with punctuation collapsed to spaces."""
    return re.sub(r"[^a-z0-9]+", " ", (title or "").casefold()).strip()


def _title_confirms_genai(title: str, description: str = "") -> bool:
    """High-confidence GenAI title that must not be lost to a generic JD."""
    text = _normalized_title(title)
    if not text or _NON_TARGET_TITLE_RE.search(text):
        return False
    if not _GENAI_TITLE_RE.search(text):
        return False

    # A clear contradiction deserves whole-listing judgement. Mere generic
    # software/recruiter boilerplate does not: that was the false-negative
    # pattern this gate was added to prevent.
    jd = _normalized_title(description)
    if jd and _NON_TARGET_JD_RE.search(jd):
        return False

    # Normal builder title, or a title consisting only of the GenAI domain
    # itself (for example Naukri listings titled simply "Gen AI").
    return bool(_BUILDER_TITLE_RE.search(text)) or text in _DOMAIN_ONLY_TITLES


def _is_ordinary_qa_role(title: str, description: str = "") -> bool:
    """True for conventional QA jobs that merely test an AI-named product."""
    title_text = _normalized_title(title)
    jd = _normalized_title(description)
    return bool(
        _QA_TITLE_RE.search(title_text)
        and _ORDINARY_QA_JD_RE.search(jd)
        and not _AI_EVALUATION_JD_RE.search(jd)
    )

SYSTEM_PROMPT = (
    "You screen job listings for a candidate targeting hands-on AI and "
    "generative-AI engineering work.\n\n"
    "Answer YES when the job's core work is building, integrating, evaluating, "
    "deploying, or operating AI systems. This includes generative AI, LLMs, "
    "RAG, agentic AI, conversational AI, NLP, machine learning, deep learning, "
    "computer vision, data science with model development, MLOps/LLMOps, AI "
    "platform engineering, and software engineering whose main product or "
    "features are AI-powered.\n\n"
    "Programming language is not a gate. Python is common, but Java, "
    "JavaScript/TypeScript, C#, Go, or another language still qualifies when "
    "hands-on AI engineering is the main work.\n\n"
    "Use the title, responsibilities, and required skills together:\n"
    "- An explicit AI/ML/GenAI title is strong evidence. Answer YES unless the "
    "description clearly proves the role is actually unrelated or AI is only "
    "marketing. Do not reject it merely because the description is broad, "
    "templated, consulting-style, or also mentions backend, cloud, data, or "
    "platform work.\n"
    "- A plain title can still be YES when the described work is mainly AI.\n"
    "- When a role is plausibly hands-on AI work but the listing is ambiguous, "
    "answer YES. Missing a real role is worse than one extra application.\n\n"
    "Answer NO when the core work is unrelated to hands-on AI engineering: "
    "generic software development, plain DevOps/cloud administration, plain "
    "data engineering/ETL/BI, IoT without model development, sales/GTM, "
    "support, recruiting, or pure product/project/people management.\n"
    "Answer NO for ordinary QA/testing of non-AI products, but YES when the "
    "core work is technical AI/LLM evaluation, red-teaming, guardrails, safety, "
    "model-quality engineering, or AI test generation.\n"
    "An AI-named QA role centered on Selenium, JMeter, Gatling, Cypress, "
    "Playwright, Appium, generic automation testing, or performance testing is "
    "NO unless its actual core is model/LLM evaluation or AI safety.\n"
    "Answer NO for policy-only AI governance, audit, risk, or compliance, but "
    "YES when the role builds technical governance, evaluation, safety, or "
    "guardrail systems.\n\n"
    "Reply with exactly one word: YES or NO."
)


def _openai_api_key() -> str:
    """Preferred key name plus compatibility with this repo's old typo."""
    return llm.api_key("OPENAI_API_KEY") or llm.api_key("OPEN_API_KEY")


def role_filter_enabled() -> bool:
    """True when the role filter is switched on, keyed, and still healthy."""
    if not config.AI_ROLE_FILTER:
        return False
    if llm.tripped(CALLER):
        return False
    return bool(_openai_api_key())


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
            "Authorization": f"Bearer {_openai_api_key()}",
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
    """Is this listing in the hands-on AI/ML/GenAI engineering target?

    Returns True for a clear GenAI builder title and False for clearly ordinary
    QA jobs; otherwise True/False from the model, or None when the filter is
    disabled, unkeyed, unhealthy, or unusable. Callers MUST treat None as
    "don't know" and fall back rather than dropping a job.
    """
    title = (title or "").strip()
    if not config.AI_ROLE_FILTER or not title:
        return None

    # Conventional QA of an AI-named product is still QA, not AI engineering.
    # Keep genuine model/LLM evaluation and AI-safety work on the model path.
    if _is_ordinary_qa_role(title, description):
        return False

    # Preserve obvious GenAI roles even when their JD is generic recruiter or
    # consulting boilerplate. Explicitly non-target titles (QA, governance,
    # sales, etc.) still require whole-listing model judgement.
    if _title_confirms_genai(title, description):
        return True

    if not role_filter_enabled():
        return None

    tag_text = ", ".join(tags or [])[:300]
    # Whole description by default (AI_ROLE_FILTER_JD_CHARS = 0). Ambiguous
    # roles need the complete work description; callers pass HTML-stripped
    # text (job_utils.plain_text).
    jd = (description or "").strip()
    if config.AI_ROLE_FILTER_JD_CHARS > 0:
        jd = jd[: config.AI_ROLE_FILTER_JD_CHARS]

    prompt = (
        f"Job title: {title}\n"
        f"Company: {(company or 'N/A').strip()}\n"
        f"Skills/tags: {tag_text or 'N/A'}\n"
        f"Description: {jd or 'N/A'}\n\n"
        "Is this a hands-on AI/ML/GenAI engineering role in the target scope? "
        "Reply YES or NO."
    )

    # Keyed on the whole prompt, not title+company. Two postings can share a
    # title and company and still describe completely different work — reusing
    # the first verdict for both would defeat reading the description at all.
    if prompt in _verdict_cache:
        return _verdict_cache[prompt]

    raw = _ask_openai(prompt)
    if not raw:
        return None

    # Tolerant of harmless punctuation/Markdown, but require the exact token.
    # ``startswith('no')`` incorrectly treated replies such as "not enough
    # information" as a confident rejection.
    reply = " ".join(raw.split()).lower().strip("`*.!,:\"' ")
    if reply == "yes":
        verdict = True
    elif reply == "no":
        verdict = False
    else:
        logger.warning("Unusable role-filter reply %r for %r", raw[:80], title)
        return None

    _verdict_cache[prompt] = verdict
    return verdict
