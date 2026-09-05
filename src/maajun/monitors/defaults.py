from __future__ import annotations

# Deliberately wide: a missed 500 is the whole point of the tool, a false
# positive costs one analysis. Warnings are excluded — apps handle those.
DEFAULT_ERROR_PATTERN = (
    r"\b(ERROR|CRITICAL|FATAL|SEVERE|EMERG|ALERT|Traceback|panic|panicked)\b"
    r"|\bUnhandled\w*"
    r"|\bFatal error\b"
    r"|\bException\b(?!\s+handled)"
    r"|\b5\d{2}\s+(Internal Server Error|Bad Gateway|Service Unavailable|Gateway Time-?out)"
)

DEFAULT_TRACEBACK_HEADERS: tuple[str, ...] = (
    "Traceback (most recent call last):",  # Python
    "Caused by:",  # Java chained exceptions
    "panic:",  # Go
    "goroutine ",  # Go
    "Exception in thread ",  # Java
    "Unhandled exception",  # .NET, and Node's older wording
    "UnhandledPromiseRejection",  # Node
    "thread '",  # Rust: thread 'main' panicked at ...
    "PHP Fatal error:",  # PHP
    "Fatal error:",  # PHP without the prefix, and some C++ runtimes
    "* Error in",  # Ruby's bug report banner
)

# Matched against when json_level_field is configured.
DEFAULT_JSON_LEVEL_VALUES: frozenset[str] = frozenset(
    {"error", "critical", "fatal", "emerg", "alert", "severe", "panic"}
)

TRACEBACK_LOOKAHEAD_LINES = 3
