"""Anti-ban helpers: randomized pacing, delays, breaks, and identifiers.

Naukri fingerprints request timing as aggressively as IPs — fixed sleep
intervals and constant markers (e.g. the old sid suffix "0000000") are
trivially detectable. All knobs live in agent_config.py; set
HUMANIZE=False to disable everything.

NOTE: run from a residential IP — no client-side randomization can
protect a datacenter IP (see src/client/naukri_client.py).
"""

import random
import threading
import time
from datetime import datetime, timezone

from src.config import agent_config as config


def human_delay(min_s: float, max_s: float) -> None:
    """Randomized sleep (triangular distribution + occasional hesitation)."""
    if not config.HUMANIZE:
        time.sleep(min_s)
        return
    duration = random.triangular(min_s, max_s)
    if random.random() < 0.10:  # humans aren't metronomes
        duration += random.uniform(0.5, 2.5)
    time.sleep(duration)


def maybe_long_break() -> None:
    """Occasional long 'human walked away' break."""
    if config.HUMANIZE and random.random() < config.LONG_BREAK_PROBABILITY:
        duration = random.uniform(config.LONG_BREAK_MIN_SECONDS, config.LONG_BREAK_MAX_SECONDS)
        print(f"☕ Human-like break: {duration:.0f}s")
        time.sleep(duration)


def reading_pause() -> None:
    """Simulates reading a job description before applying."""
    if config.HUMANIZE:
        human_delay(config.READING_PAUSE_MIN_SECONDS, config.READING_PAUSE_MAX_SECONDS)


def session_warmup() -> None:
    """Random idle delay before the first API call (breaks fixed cron start times)."""
    if config.HUMANIZE:
        duration = random.uniform(config.SESSION_WARMUP_MIN_SECONDS, config.SESSION_WARMUP_MAX_SECONDS)
        print(f"🕒 Warm-up delay: {duration:.1f}s")
        time.sleep(duration)


def generate_sid() -> str:
    """Timestamp + 7 random digits (never a constant-suffix fingerprint)."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return stamp + "".join(random.choices("0123456789", k=7))


def jitter_int(value: int, spread: int) -> int:
    """value +/- random(0..spread), never below 1."""
    return max(1, value + random.randint(-spread, spread)) if spread > 0 else value


def shuffled(items: list) -> list:
    """Shuffled copy (original untouched)."""
    out = list(items)
    random.shuffle(out)
    return out


class RequestPacer:
    """Randomized minimum gap between API calls + adaptive 403/429 cooldown."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last = 0.0
        self._penalty = 0.0
        self.blocks_seen = 0

    def pace(self, kind: str = "api") -> None:
        """Call right before every outgoing API request."""
        if not config.HUMANIZE:
            return
        with self._lock:
            gap = random.uniform(config.MIN_REQUEST_GAP_SECONDS, config.MAX_REQUEST_GAP_SECONDS)
            gap += self._penalty
            if random.random() < 0.15:  # occasional extra hesitation
                gap += random.uniform(1.0, 4.0)
            wait = gap - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()

    def register_block(self, status_code=None, cooldown: bool = True) -> None:
        """Record a 403/429: slow the whole run down and cool off (escalating)."""
        with self._lock:
            self.blocks_seen += 1
            self._penalty = min(self._penalty + random.uniform(3.0, 8.0),
                                config.MAX_BLOCK_PENALTY_SECONDS)
        if cooldown and config.HUMANIZE:
            duration = random.uniform(config.BLOCK_COOLDOWN_MIN_SECONDS,
                                      config.BLOCK_COOLDOWN_MAX_SECONDS) * min(self.blocks_seen, 4)
            print(f"🧊 Server pushback (HTTP {status_code}). Cooling down {duration:.0f}s...")
            time.sleep(duration)


PACER = RequestPacer()
pace = PACER.pace
register_block = PACER.register_block


def too_many_blocks() -> bool:
    """True once the run has hit the configured server-block abort threshold."""
    return PACER.blocks_seen >= config.MAX_BLOCKS_BEFORE_ABORT
