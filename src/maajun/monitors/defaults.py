from __future__ import annotations

DEFAULT_ERROR_PATTERN = r"\b(ERROR|CRITICAL|FATAL)\b"

DEFAULT_TRACEBACK_HEADERS: tuple[str, ...] = (
    "Traceback (most recent call last):",  # Python
    "Caused by:",  # Java chained exceptions
    "panic:",  # Go
    "goroutine ",  # Go
    "Exception in thread ",  # Java
)

# Matched against when json_level_field is configured.
DEFAULT_JSON_LEVEL_VALUES: frozenset[str] = frozenset({"error", "critical", "fatal"})

TRACEBACK_LOOKAHEAD_LINES = 3
