"""Detection defaults shared by the log monitor and the config model.

Kept in their own module so `config` does not have to import a monitor
implementation just to name a default — the dependency ran the wrong way.
"""

from __future__ import annotations

# Only genuine failures by default. Warnings are common and mostly benign, and
# every matched line costs an AI call and a report — opt in explicitly via
# monitor.error_pattern if you want them.
DEFAULT_ERROR_PATTERN = r"\b(ERROR|CRITICAL|FATAL)\b"

# Lines that open a multi-line stack trace, per language.
DEFAULT_TRACEBACK_HEADERS: tuple[str, ...] = (
    "Traceback (most recent call last):",  # Python
    "Caused by:",  # Java chained exceptions
    "panic:",  # Go
    "goroutine ",  # Go
    "Exception in thread ",  # Java
)

# Levels that JSON-formatted logs are matched against when
# json_level_field is configured.
DEFAULT_JSON_LEVEL_VALUES: frozenset[str] = frozenset({"error", "critical", "fatal"})

# How many lines after an error line may be scanned for a traceback header.
# Bounded deliberately: an unbounded scan merges an error with an unrelated
# traceback far below it and swallows everything in between.
TRACEBACK_LOOKAHEAD_LINES = 3
