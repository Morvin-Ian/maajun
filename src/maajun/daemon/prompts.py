"""Prompt templates for incident analysis and manual reports."""

ANALYZE_PROMPT = """\
An error was detected on a monitored system. Investigate it against the
repository checked out at {workspace} and write an incident report.

Error source: {source}
First seen: {timestamp}

Error details:
```
{details}
```

Use the read_file/grep/glob/list_dir tools on {workspace} to locate the
code involved. Then respond with ONLY a markdown report in this format:

# <one-line error summary>

## What happened
<plain-language description of the error>

## Root cause
<your analysis, referencing files and lines in the repo>

## Likely cause commit
<the commit below that most plausibly introduced this, with one line of
reasoning; write "Unclear" if none of them touch the code involved>

## Suggested fix
<concrete change(s), with code snippets where helpful>
"""

RECENT_COMMITS_SECTION = """
Recent commits on {branch}, newest first — one of these may have introduced
the error. Use git_status and read_file to check what they touched; do not
guess from the subject line alone.

```
{commits}
```
"""

FIX_PROMPT_SUFFIX = """
You MAY apply the fix: use edit_file/write_file on files inside {workspace}.
Keep the change minimal and focused on this error. Still finish by
responding with the markdown report, adding an "## Applied fix" section
describing exactly what you changed.
"""

MANUAL_REPORT_PROMPT = """\
A user reported the following issue. Investigate it against the
repository checked out at {workspace} and write an incident report.

Issue description:
```
{description}
```

Use the read_file/grep/glob/list_dir tools on {workspace} to locate the
code involved. Then respond with ONLY a markdown report in this format:

# <one-line error summary>

## What happened
<plain-language description of the issue>

## Root cause
<your analysis, referencing files and lines in the repo>

## Suggested fix
<concrete change(s), with code snippets where helpful>
"""
