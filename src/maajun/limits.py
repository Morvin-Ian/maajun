from __future__ import annotations

# A published incident that goes quiet for this long and then happens again
# is treated as a regression, not as more of the same.
DEFAULT_REOPEN_AFTER_DAYS = 7.0
