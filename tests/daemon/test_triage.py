"""A guard that refused bad input is not a bug worth filing.

Three passes decide: the signatures here, before any model is asked; one
cheap tool-less question for the guards no signature can recognise; and the
agent's own verdict on the finished report. All of them have to fail open —
an error nobody can classify is a defect until shown otherwise.
"""

import pytest

from maajun.daemon.reports import BY_DESIGN, DEFECT, by_design_reason, verdict
from maajun.daemon.triage import by_design, compile_extra, screened_out

# ---------------------------------------------------------------------------
# Signatures
# ---------------------------------------------------------------------------

INTENDED = [
    "ValidationError: email is not a valid address",
    "django.core.exceptions.ValidationError: {'age': ['Enter a whole number.']}",
    "POST /api/login 401 Unauthorized",
    "AuthenticationFailed: token could not be decoded",
    "jwt.ExpiredSignatureError: Signature has expired",
    "GET /admin 403 Forbidden",
    "PermissionDenied: user 41 may not edit this record",
    "CSRF verification failed. Request aborted.",
    "429 Too Many Requests — retry after 30s",
    "RateLimitExceeded: 100 requests per minute",
    "GET /items/9999 404 Not Found",
]

DEFECTS = [
    "IndexError: list index out of range",
    "KeyError: 'discount'",
    "psycopg2.OperationalError: connection to server was lost",
    "TypeError: unsupported operand type(s) for +: 'int' and 'str'",
    "500 Internal Server Error",
    "OSError: [Errno 28] No space left on device",
]


@pytest.mark.parametrize("details", INTENDED)
def test_a_guard_naming_its_own_intent_is_not_a_defect(details):
    assert by_design(details) != ""


@pytest.mark.parametrize("details", DEFECTS)
def test_a_real_failure_is_still_a_defect(details):
    assert by_design(details) == ""


def test_the_reason_says_which_guard_fired():
    """It ends up on the incident, so it has to mean something to a reader."""
    assert "validation" in by_design("ValidationError: bad email")
    assert "rate limiter" in by_design("429 Too Many Requests")
    assert "authenticated" in by_design("401 Unauthorized")


def test_the_signatures_can_be_turned_off_entirely():
    """For a codebase where these are genuinely bugs."""
    assert by_design("ValidationError: bad email", use_defaults=False) == ""


def test_a_codebase_can_name_its_own():
    """The shipped signatures cannot know about an app's own guards."""
    extra = compile_extra([r"PaywallError"])
    assert by_design("PaywallError: plan does not include exports", extra) != ""
    assert "PaywallError" in by_design("PaywallError: no export on this plan", extra)


def test_a_users_pattern_is_tried_before_the_shipped_ones():
    extra = compile_extra([r"ValidationError"])
    assert "ignore_patterns" in by_design("ValidationError: x", extra)


def test_an_unparseable_pattern_is_skipped_not_fatal():
    """A typo in a config file must not stop the daemon watching."""
    extra = compile_extra(["([unclosed", r"PaywallError"])
    assert len(extra) == 1
    assert by_design("PaywallError: nope", extra) != ""


def test_nothing_matches_an_empty_error():
    assert by_design("") == ""


# ---------------------------------------------------------------------------
# The agent's verdict
# ---------------------------------------------------------------------------


def report(line: str) -> str:
    return f"# a finding\n\n## Verdict\n{line}\n\n## Root cause\nsomething\n"


def test_the_agent_can_call_an_error_intended():
    assert verdict(report("by design — the serializer rejects a bad email")) == BY_DESIGN


def test_a_hyphenated_verdict_is_read_the_same_way():
    assert verdict(report("by-design, the quota is meant to refuse this")) == BY_DESIGN


def test_a_defect_verdict_is_read_as_a_defect():
    assert verdict(report("defect — the caller never checks for None")) == DEFECT


def test_markdown_around_the_verdict_does_not_hide_it():
    assert verdict(report("**by design** — deliberate")) == BY_DESIGN


def test_a_missing_verdict_is_not_by_design():
    """Silence must not suppress a report: an unclassified error is a defect
    until something says otherwise."""
    assert verdict("# a finding\n\n## Root cause\nsomething\n") == ""


def test_an_unreadable_verdict_is_not_by_design():
    assert verdict(report("hard to say, could be either")) == ""


def test_the_reason_carried_onto_the_incident_is_the_agents_own_line():
    text = report("by design — the paywall refuses exports on the free plan")
    assert "paywall" in by_design_reason(text)


def test_a_verdict_with_no_line_still_yields_a_reason():
    assert by_design_reason("## Verdict\n\n## Root cause\nx") == BY_DESIGN


# ---------------------------------------------------------------------------
# Reading the screen's one line
# ---------------------------------------------------------------------------


def test_investigate_is_not_a_reason_to_skip():
    assert screened_out("investigate") == ""


def test_a_by_design_verdict_carries_its_reason():
    assert "a rate limiter refused it" in screened_out(
        "by design: a rate limiter refused it"
    )


def test_formatting_around_the_verdict_is_tolerated():
    assert screened_out("`By Design - quota exhausted`")
    assert screened_out("**by design**: paywall")


def test_a_verdict_buried_in_prose_is_not_a_verdict():
    """A screen that cannot make itself understood must not drop an error."""
    assert screened_out("I think this is by design, but I cannot be sure") == ""
    assert screened_out("") == ""
    assert screened_out("hmm") == ""


def test_a_bare_by_design_still_reads_as_one():
    assert screened_out("by design") == (
        "the screen read it as by design: no reason given"
    )
