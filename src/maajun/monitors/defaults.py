from __future__ import annotations

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
TRACEBACK_LOOKAHEAD_LINES = 3
