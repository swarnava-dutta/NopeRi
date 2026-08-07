"""Shared plumbing for the two LLM call sites.

``ai_answer`` (questionnaire answers, Anthropic) and ``ai_role_filter``
(AI/ML role verdict, OpenAI) had identical .env loading, POST-and-fail-soft
handling, and consecutive-failure trip switches. Only that duplication lives
here — each caller still owns its own prompt, payload shape, and reply
parsing, because those are genuinely different per provider.

Every failure path returns ``None`` so callers fall back to their own rules
instead of a missing opinion silently changing behaviour.
"""

import logging
import os

import requests
from dotenv import load_dotenv

from src.config import agent_config as config

load_dotenv()

logger = logging.getLogger(__name__)

# Consecutive failures per call site. Reset on the first success.
_failures: dict[str, int] = {}


class NoFault(Exception):
    """Reply unusable, but the provider is healthy — don't count a failure.

    Raised from a caller's ``extract``. ``call`` still returns None, because
    the reply really is unusable, but the consecutive-failure counter is left
    untouched. Without this, a run of successful-but-unusable responses (e.g.
    a reasoning model spending its whole token budget before writing anything)
    trips the AI_MAX_FAILURES breaker and silently disables the call site for
    the rest of the run — which looks like the filter working, not failing.
    """


def api_key(env_var: str) -> str:
    return os.getenv(env_var, "").strip()


def tripped(name: str) -> bool:
    """True once this call site has failed AI_MAX_FAILURES times in a row."""
    return _failures.get(name, 0) >= config.AI_MAX_FAILURES


def call(name: str, url: str, headers: dict, payload: dict, extract,
         timeout: float | None = None) -> str | None:
    """POST ``payload`` and return the reply text, or None on any failure.

    ``extract`` maps the provider's response JSON to the reply text; raising
    inside it is treated as a failure like any other, except for ``NoFault``,
    which returns None without counting against the breaker.

    ``timeout`` defaults to ``AI_TIMEOUT_SECONDS``. Callers using a reasoning
    model override it: thinking time scales with reasoning_effort, and a
    timeout counts against the breaker, so one shared value can't serve both
    a quick questionnaire answer and a deliberated verdict.
    """
    try:
        res = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=timeout or config.AI_TIMEOUT_SECONDS,
        )

        if res.ok:
            text = extract(res.json())
        else:
            logger.warning("%s error %s: %s", name, res.status_code, res.text[:300])
            text = None
    except NoFault as exc:  # unusable reply, healthy provider — see above
        logger.warning("%s: %s", name, exc)
        return None
    except Exception as exc:  # network errors, timeouts, bad JSON
        logger.warning("%s call failed: %s", name, exc)
        text = None

    if text:
        _failures[name] = 0
        return text

    _failures[name] = _failures.get(name, 0) + 1
    return None
