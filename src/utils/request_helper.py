import functools
import logging
import random
import time

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Retry configuration
# ---------------------------------------------------------------------------
RETRY_MAX_ATTEMPTS = 5       # total attempts (1 original + 4 retries)
RETRY_BASE_DELAY   = 1.0     # seconds — delay before the 1st retry
RETRY_MAX_DELAY    = 60.0    # seconds — cap on any single sleep
RETRY_MULTIPLIER   = 2.0     # exponential growth factor
RETRY_JITTER       = 0.3     # fraction of delay added as random jitter

# HTTP statuses treated as transient
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


def with_exponential_retry(
    max_attempts: int = RETRY_MAX_ATTEMPTS,
    base_delay: float = RETRY_BASE_DELAY,
    max_delay: float = RETRY_MAX_DELAY,
    multiplier: float = RETRY_MULTIPLIER,
    jitter: float = RETRY_JITTER,
    reraise_as=None,
    label: str = "request",
    retry_statuses=None,
):
    """Decorator that wraps any method with exponential-backoff retry logic.

    The wrapped method is retried when it either raises an exception, or
    returns a response object with a transient HTTP status (429 / 5xx).

    ``retry_statuses`` can override the default set of HTTP statuses that
    trigger a retry (e.g. only 5xx so 429s are handled by the caller).
    """
    statuses = set(retry_statuses) if retry_statuses is not None else _RETRYABLE_STATUSES

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            delay = base_delay

            for attempt in range(1, max_attempts + 1):
                try:
                    result = func(*args, **kwargs)

                    # If the call returned a response, check the status.
                    if getattr(result, "status_code", None) in statuses:
                        logger.warning(
                            "[%s] attempt %d/%d — HTTP %d, retrying in %.1fs …",
                            label, attempt, max_attempts, result.status_code, delay,
                        )
                        # On last attempt just return the bad response so the
                        # caller can inspect it.
                        if attempt == max_attempts:
                            return result
                    else:
                        return result  # success

                except Exception as exc:
                    if attempt == max_attempts:
                        logger.error(
                            "[%s] all %d attempts failed. Last error: %s",
                            label, max_attempts, exc,
                        )
                        if reraise_as:
                            raise reraise_as(str(exc)) from exc
                        raise

                    logger.warning(
                        "[%s] attempt %d/%d failed (%s: %s), retrying in %.1fs …",
                        label, attempt, max_attempts, type(exc).__name__, exc, delay,
                    )

                # Exponential back-off with jitter
                time.sleep(min(delay * (1 + jitter * random.random()), max_delay))
                delay = min(delay * multiplier, max_delay)

        return wrapper
    return decorator
