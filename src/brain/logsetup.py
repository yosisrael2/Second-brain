"""Agent logging setup (email-agent/info-log).

launchd jobs start with NO logging configuration: the root logger sits at
WARNING with only the last-resort stderr handler, so INFO records — the
gmail-poll per-cycle line ("gmail-poll: state=ok listed=... new=...") —
vanish and an operator sees nothing in ~/Library/Logs/second-brain/
email.log between warnings.

The email agent's plists route stdout to ``email.log`` and stderr to
``email.err.log`` (scripts/service.sh), so the gmail-poll dispatcher
installs an INFO-level stdout root handler once. Agents that configure
logging themselves (telegram does; api/whatsapp/connectors have their own
entry setup) keep theirs byte-identical: this setup is a no-op when the
root logger already has handlers.

Secrets discipline: the cycle line carries counts only (state / listed /
new / skipped / errors — never tokens, never addresses). Noisy third-party
loggers whose records can embed URLs or query strings are pinned to
WARNING so nothing new becomes visible at INFO.
"""

from __future__ import annotations

import logging
import sys

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

# Third-party loggers that may embed URLs (query strings, tokens in
# request lines) at INFO/DEBUG — never let the new verbosity surface them.
_PINNED_TO_WARNING = ("httpx", "httpcore", "googleapiclient", "google", "urllib3")


def configure_agent_logging(stream: object | None = None) -> None:
    """Give the process an INFO root handler on stdout (the launchd
    ``email.log`` stream), once.

    No-op when the root logger already has handlers: an agent that
    configured its own logging (telegram's basicConfig) must stay
    byte-identical.
    """
    root = logging.getLogger()
    if root.handlers:
        return  # the entry point already configured logging — keep theirs
    handler = logging.StreamHandler(stream=stream or sys.stdout)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    handler._brain_agent = True  # test marker: ours, not a third-party handler
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    for name in _PINNED_TO_WARNING:
        logging.getLogger(name).setLevel(logging.WARNING)
