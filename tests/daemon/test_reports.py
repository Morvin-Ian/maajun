"""The artifact title has to name what the report says to fix.

Titling from the raw log line instead was the mismatch these cover: the
exception surfaces in one place and the defect is regularly in another, so an
issue called `KeyError: 'discount'` would send a reader to the wrong file.
"""

from maajun.daemon.reports import (
    artifact_title,
    commit_subject,
    extract_patches,
    headline,
    headline_problem,
)

REPORT = """# cart/totals.py assumes promotions.apply() always writes a discount

## What happened
Checkout returned a 500 for carts with no matching promotion.

## Root cause
`cart/totals.py:88` reads `cart["discount"]` directly.

## Suggested fix
Use `cart.get("discount", Decimal("0"))`.
"""

RAW_ERROR = "KeyError: 'discount'"


# ---------------------------------------------------------------------------
# Reading the headline
# ---------------------------------------------------------------------------


def test_the_headline_is_the_reports_own_first_line():
    assert headline(REPORT) == (
        "cart/totals.py assumes promotions.apply() always writes a discount"
    )


def test_backticks_and_bold_are_stripped_out_of_the_title():
    """They render as literal characters in a GitHub title."""
    assert headline("# `main.py:12` indexes an **empty** list") == (
        "main.py:12 indexes an empty list"
    )


def test_a_deeper_heading_is_read_when_the_model_skips_the_h1():
    assert headline("## main.py indexes an empty list") == (
        "main.py indexes an empty list"
    )


def test_trailing_hashes_are_not_part_of_the_title():
    assert headline("# main.py indexes an empty list ###") == (
        "main.py indexes an empty list"
    )


def test_the_unfilled_template_line_is_not_a_headline():
    """Echoing the format back would title the issue with the instructions."""
    assert headline("# <one line: the defect and the file it is in>") == ""


def test_a_report_with_no_heading_has_no_headline():
    assert headline("The code looks fine to me.") == ""
    assert headline("") == ""


def test_the_first_heading_wins_over_the_section_headings():
    assert headline(REPORT) != "What happened"


# ---------------------------------------------------------------------------
# Building the title
# ---------------------------------------------------------------------------


def test_the_title_names_the_defect_not_the_exception():
    assert artifact_title(REPORT, RAW_ERROR) == (
        "[maajun] cart/totals.py assumes promotions.apply() always writes a discount"
    )


def test_the_raw_error_is_only_the_fallback():
    assert artifact_title("no heading here", RAW_ERROR) == "[maajun] KeyError: 'discount'"


def test_a_long_headline_is_cut_rather_than_rejected():
    long = "# " + "a very long finding " * 20
    title = artifact_title(long, RAW_ERROR)
    assert len(title) < 100
    assert title.endswith("…")


def test_the_commit_subject_names_the_same_defect_as_the_title():
    """A reviewer reading `git log` and one reading the PR see one story."""
    title = artifact_title(REPORT, RAW_ERROR)
    subject = commit_subject(REPORT, RAW_ERROR, "maajun: incident report for")

    assert subject.startswith("maajun: incident report for ")
    shared = headline(REPORT)[:40]
    assert shared in title
    assert shared in subject


def test_the_commit_subject_falls_back_with_the_title():
    """Both fall back together, so they cannot disagree."""
    subject = commit_subject("no heading", RAW_ERROR, "maajun:")
    assert artifact_title("no heading", RAW_ERROR) == "[maajun] KeyError: 'discount'"
    assert subject == "maajun: KeyError: 'discount'"


# ---------------------------------------------------------------------------
# Earning a re-ask
# ---------------------------------------------------------------------------


def test_a_report_with_no_headline_earns_one_re_ask():
    assert headline_problem("## Root cause\nsomething") != ""
    assert "title" in headline_problem("## Root cause\nsomething")


def test_a_titled_report_is_left_alone():
    assert headline_problem(REPORT) == ""


def test_a_dry_run_shows_the_title_it_would_file(capsys):
    """The one place to catch a title that disagrees with the report."""
    from maajun.daemon.reports import print_dry_run

    print_dry_run(
        "AI analysis", "owner/name", REPORT, (10, 5, 0.001),
        title=artifact_title(REPORT, RAW_ERROR),
    )
    out = capsys.readouterr().out
    assert "Would be titled: [maajun] cart/totals.py assumes" in out


# ---------------------------------------------------------------------------
# Reading the patch back out of the report
# ---------------------------------------------------------------------------

MAIN_PY_PATCH = """```diff
--- a/main.py
+++ b/main.py
@@ -1 +1 @@
-items = []
+items = [0]
```"""


def test_a_tagged_diff_fence_is_extracted_whole():
    report = REPORT + "\n\n" + MAIN_PY_PATCH

    patches = extract_patches(report)

    assert len(patches) == 1
    assert patches[0].startswith("--- a/main.py")
    # git apply wants the trailing newline a trimmed fence loses.
    assert patches[0].endswith("+items = [0]\n")


def test_an_untagged_patch_is_still_found():
    block = MAIN_PY_PATCH.replace("```diff", "```")
    assert extract_patches(REPORT + "\n" + block) != []


def test_a_new_file_patch_is_recognized():
    block = """```
--- /dev/null
+++ b/handlers/cart.py
@@ -0,0 +1 @@
+def total(cart): ...
```"""
    assert len(extract_patches(block)) == 1


def test_multiple_patches_come_out_in_order():
    second = MAIN_PY_PATCH.replace("main.py", "totals.py")
    patches = extract_patches(MAIN_PY_PATCH + "\n" + second)
    assert [p.splitlines()[0] for p in patches] == [
        "--- a/main.py", "--- a/totals.py",
    ]


def test_prose_and_code_blocks_are_never_patches():
    """A block that merely shows before/after lines must not reach git."""
    prose = "```\nThe fix is to guard the access in main.py.\n```"
    code = """```python
-items = []
+items = [0]
```"""
    assert extract_patches(prose) == []
    assert extract_patches(code) == []


def test_a_report_without_fences_yields_nothing():
    assert extract_patches(REPORT) == []
    assert extract_patches("") == []
