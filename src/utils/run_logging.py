"""Single place that configures run logging.

Two things were wrong before this module existed:

1. Every module builds a ``logging.getLogger(__name__)`` and calls
   ``logger.debug``/``warning``, but no handler was ever installed. Python's
   last-resort handler only emits WARNING and above to stderr, so every
   ``logger.debug`` — including the raw apply payload in
   ``NaukriJobClient.apply_job`` — was silently discarded exactly when it was
   needed to diagnose a failing run.

2. ``Run Noperi.bat`` appended to one log file from two writers with
   different encodings: cmd's ``echo`` (ANSI) and PowerShell 5.1's
   ``Tee-Object`` (UTF-16LE). The result was a file that is ~48% NUL bytes
   and unreadable in any single encoding.

The batch file now only writes UTF-8 markers, and this module owns the
rotating diagnostic log. Console output stays exactly as it was: the emoji
``print`` lines the agent already produces are the human-facing run report,
so handlers here never duplicate them onto stdout.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

from src.config import agent_config as config

_CONFIGURED = False

# Third-party libraries are chatty at DEBUG (every socket, every retry) and
# would bury our own records.
_NOISY_LIBRARIES = (
    "urllib3",
    "httpcloak",
    "websocket",
    "charset_normalizer",
    "selenium",
    "requests",
)


def _resolve_level(name: str, default: int) -> int:
    level = logging.getLevelName(str(name).strip().upper())
    return level if isinstance(level, int) else default


def setup_logging() -> logging.Logger:
    """Install the file handler once and return the root logger.

    Safe to call repeatedly; later calls are a no-op so importing a module
    twice can never attach duplicate handlers.
    """
    global _CONFIGURED

    root = logging.getLogger()
    if _CONFIGURED:
        return root

    if not getattr(config, "LOG_TO_FILE", True):
        _CONFIGURED = True
        return root

    file_level = _resolve_level(getattr(config, "LOG_FILE_LEVEL", "DEBUG"), logging.DEBUG)
    console_level = _resolve_level(
        getattr(config, "LOG_CONSOLE_LEVEL", "ERROR"), logging.ERROR
    )

    log_path = os.path.abspath(getattr(config, "LOG_FILE", "logs/noperi_debug.log"))
    os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)

    handler = RotatingFileHandler(
        log_path,
        maxBytes=int(getattr(config, "LOG_MAX_BYTES", 2_000_000)),
        backupCount=int(getattr(config, "LOG_BACKUP_COUNT", 3)),
        encoding="utf-8",
    )
    handler.setLevel(file_level)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    # The root logger must pass the lowest level any handler wants, otherwise
    # records are dropped before a handler ever sees them.
    root.setLevel(min(file_level, console_level))
    root.addHandler(handler)

    # Only genuine problems reach the console, so the emoji run report the
    # agent prints stays readable.
    console = logging.StreamHandler()
    console.setLevel(console_level)
    console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    root.addHandler(console)

    for name in _NOISY_LIBRARIES:
        logging.getLogger(name).setLevel(logging.WARNING)

    _CONFIGURED = True
    logging.getLogger(__name__).debug("Logging initialised → %s", log_path)
    return root
