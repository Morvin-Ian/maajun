from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

# (reason, pattern), each named after its own intent. Anything needing to
# know the application to judge belongs in the agent's verdict, not here.
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


# The middle pass: asks a cheap model what no signature can recognise, before
# a full investigation files nothing. Biased hard towards investigating.
SCREEN_PROMPT = """\
You are triaging one error from a running application before a full
investigation is paid for. You have no tools and cannot read the code — judge
only what the error itself says.

Error source: {source}

```
{details}
```

Answer with one line, and nothing else:

- `investigate` — this looks like a defect, or you cannot tell from the error
  alone. Any doubt at all is this answer.
- `by design: <reason>` — the application refused something on purpose and
  reported the refusal as an error. Input that failed validation, a login
  refused, a rate limit, a quota or plan check, a paywall, a feature flag off,
  a guard clause rejecting the state it exists to reject, a 404 for a row that
  was never there.

An unhandled exception through application code is `investigate`, even when
the value that caused it came from bad input: the check being wrong, or
missing, is the defect. A refusal that escapes as a 500 is `investigate` too
— the refusal was intended, crashing on it was not.
"""

BY_DESIGN_REPLY = "by design"


def screened_out(answer: str) -> str:
    """The screen's reason for skipping this error, or "" to investigate.

    Anything unexpected reads as "investigate": a screen that cannot make
    itself understood must not be what drops an error.
    """
    lines = (answer or "").strip().splitlines()
    line = lines[0].strip().strip("`*").lower() if lines else ""
    if not line.startswith(BY_DESIGN_REPLY):
        return ""
    reason = line[len(BY_DESIGN_REPLY):].lstrip(":- ").strip()
    return f"the screen read it as by design: {reason or 'no reason given'}"
