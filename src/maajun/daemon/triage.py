"""Telling a defect from a guard that fired the way it was built to.

Not every logged error is a bug. A rejected login, a validation failure, a
429 from a rate limiter, a 404 for a row that was never there — the code did
exactly what it is meant to do, and there is nothing to fix. Filing an issue
for one costs a reader's attention and buries the errors that are real.

Two passes catch them. This module is the cheap one: signatures matched
against the raw error before any model is asked, so an obvious guard never
becomes a billed analysis. It is deliberately narrow — it can only recognise
errors that are named after their own intent. The second pass is the agent's
own verdict on the report, in `reports.verdict`, which is what catches a
guard specific to the application: a paywall, a feature flag, a quota.

Nothing is dropped. A match is recorded against the incident with its reason
and listed by `maajun incidents --ignored`, so a signature that turns out to
be wrong for a codebase is visible rather than silent.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

# (reason, pattern). Each has to be named after its own intent — a guard that
# announces what it refused. Anything needing to know the application to
# judge belongs in the agent's verdict, not here.
BY_DESIGN_SIGNATURES: tuple[tuple[str, str], ...] = (
    ("input failed validation", r"\bValidation(Error|Exception|Failed)\b"),
    ("input failed validation", r"\b(400|422)\s+(Bad Request|Unprocessable)"),
    ("the request was not authenticated", r"\b401\s+Unauthorized\b"),
    # \w* on each: these arrive as often with an Error/Exception suffix as
    # without, and \b would refuse the suffixed form.
    ("the request was not authenticated",
     r"\b(Authentication(Failed|Error)|InvalidCredentials\w*|InvalidToken\w*"
     r"|ExpiredSignature\w*|TokenExpired\w*)"),
    ("the request was refused by a permission check",
     r"\b(403\s+Forbidden|PermissionDenied\w*|NotAuthorized\w*|AccessDenied\w*)"),
    ("a CSRF check refused the request", r"\bCSRF\b"),
    ("a rate limiter refused the request",
     r"\b(429\s+Too Many Requests|RateLimit\w*|Throttled)\b"),
    ("nothing was found at that address", r"\b404\s+Not Found\b"),
)

COMPILED: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (reason, re.compile(pattern, re.IGNORECASE)) for reason, pattern in BY_DESIGN_SIGNATURES
)


def compile_extra(patterns: list[str]) -> list[tuple[str, re.Pattern[str]]]:
    """The user's own signatures, skipping any that will not compile.

    A bad regex in a config file must not stop the daemon watching: the
    error is logged and the rest of the list still applies.
    """
    compiled = []
    for pattern in patterns:
        try:
            compiled.append((f"matched ignore_patterns {pattern!r}", re.compile(pattern)))
        except re.error as exc:
            log.warning("ignoring unparseable monitor.ignore_patterns entry %r: %s",
                        pattern, exc)
    return compiled


def by_design(
    details: str,
    extra: list[tuple[str, re.Pattern[str]]] | None = None,
    use_defaults: bool = True,
) -> str:
    """Why this error looks intended, or "" when it looks like a defect.

    The user's own patterns are tried first, so a codebase can name something
    the shipped signatures do not know about.
    """
    for reason, pattern in list(extra or ()) + (list(COMPILED) if use_defaults else []):
        if pattern.search(details):
            return reason
    return ""
