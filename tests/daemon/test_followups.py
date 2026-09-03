from maajun.daemon.followups import MAX_FOLLOW_UP_ISSUES, parse_follow_ups

VALID = """### Guard empty order line access
- Evidence: `handlers/orders.py:44` reads `lines[0]` although callers allow an empty list.
- Change: Return the established empty-order response before indexing the collection.
- Acceptance: A regression test passes for an order whose lines collection is empty.
"""


def test_parses_an_actionable_task():
    parsed = parse_follow_ups(VALID)

    assert parsed.invalid == ()
    assert parsed.tasks[0].title == "Guard empty order line access"
    assert parsed.tasks[0].evidence.startswith("`handlers/orders.py:44`")


def test_none_means_there_is_no_follow_up():
    assert parse_follow_ups("## Follow-up\nNone").tasks == ()
    assert parse_follow_ups("## Follow-up\nNone").invalid == ()


def test_free_form_text_is_invalid_instead_of_becoming_an_issue():
    parsed = parse_follow_ups("Maybe investigate the other handlers later.")

    assert parsed.tasks == ()
    assert "one '### action' block" in parsed.invalid[0].problems[0]


def test_requires_evidence_change_and_acceptance():
    parsed = parse_follow_ups("### Fix the other handler\n- Change: Make it safer.")

    assert parsed.tasks == ()
    assert "missing evidence field" in parsed.invalid[0].problems
    assert "missing acceptance field" in parsed.invalid[0].problems


def test_rejects_commentary_outside_the_structured_fields():
    parsed = parse_follow_ups(
        VALID + "This may also be an environment problem that needs investigation.\n"
    )

    assert parsed.tasks == ()
    assert any("outside" in problem for problem in parsed.invalid[0].problems)


def test_rejects_environment_and_missing_evidence_commentary():
    noisy = """### Document the SMTP environment
- Evidence: `settings.py:10` has a mail setting but the traceback is missing.
- Change: Add more environment commentary about the SMTP service configuration.
- Acceptance: The documentation shows the missing environment value.
"""
    parsed = parse_follow_ups(noisy)

    assert parsed.tasks == ()
    assert any("non-actionable context" in problem for problem in parsed.invalid[0].problems)


def test_preserves_valid_tasks_when_another_task_is_invalid():
    parsed = parse_follow_ups(
        VALID + "\n### Maybe look around\n- Evidence: unknown\n- Change: investigate\n"
    )

    assert len(parsed.tasks) == 1
    assert len(parsed.invalid) == 1


def test_free_form_prefix_is_invalid_without_discarding_valid_tasks():
    parsed = parse_follow_ups("Remember to clean this up later.\n\n" + VALID)

    assert len(parsed.tasks) == 1
    assert len(parsed.invalid) == 1
    assert "free-form text" in parsed.invalid[0].problems[0]


def test_rejects_work_already_included_in_the_fix():
    included = """### Add the completed guard
- Evidence: `handlers/orders.py:44` now checks the empty collection.
- Change: Keep the guard already included in this PR.
- Acceptance: A regression test passes for an empty collection.
"""

    parsed = parse_follow_ups(included)

    assert parsed.tasks == ()
    assert any("non-actionable context" in problem for problem in parsed.invalid[0].problems)


def test_exact_duplicate_tasks_are_removed():
    assert len(parse_follow_ups(VALID + "\n" + VALID).tasks) == 1


def test_issue_cap_is_intentionally_small():
    assert MAX_FOLLOW_UP_ISSUES == 3
