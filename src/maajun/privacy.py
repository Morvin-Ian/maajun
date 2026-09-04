"""Privacy-safe, stable runtime evidence.

Runtime logs are useful because they carry the route, status and failure text.
They also routinely carry credentials and user identifiers that do not belong
in a model prompt or a public GitHub artifact.  Sanitising at ``ErrorEvent``
construction gives every downstream consumer the safe form by default.
"""

from __future__ import annotations

import ipaddress
import re

JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
BEARER_RE = re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]+")
STANDALONE_SECRET_RE = re.compile(
    r"(?ix)\b(?:"
    r"sk-[a-z0-9_-]{16,}|"
    r"gh[pousr]_[a-z0-9]{20,}|"
    r"github_pat_[a-z0-9_]{20,}|"
    r"AKIA[0-9A-Z]{16}"
    r")\b"
)
PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?"
    r"-----END [^-\r\n]*PRIVATE KEY-----",
    re.DOTALL,
)
SECRET_HEADER_RE = re.compile(
    r"(?i)([\"']?\b(?:authorization|proxy-authorization|cookie|set-cookie)\b"
    r"[\"']?\s*:\s*)(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^,}\r\n]*)"
)
SECRET_FIELD_RE = re.compile(
    r"(?ix)(\b[\"']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"password|client[_-]?secret)[\"']?\s*[:=]\s*)"
    r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;\]}]+)"
)
REQUEST_BODY_RE = re.compile(
    r"(?i)(\b(?:request[_ -]?body|upload[_ -]?contents?|"
    r"formdata contents?)\b\s*[:=]\s*)[^\r\n]+"
)
URL_CREDENTIAL_RE = re.compile(
    r"(?i)(\b[a-z][a-z0-9+.-]*://[^\s/:@]+:)([^\s/@]+)(@)"
)
IPV4_RE = re.compile(
    r"(?<![\w.])(?:25[0-5]|2[0-4]\d|1?\d?\d)"
    r"(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}(?![\w.])"
)
IPV6_RE = re.compile(
    r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{0,4}:){2,7}"
    r"[0-9A-Fa-f]{0,4}(?![0-9A-Fa-f:])"
)
UUID_RE = re.compile(
    r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\b"
)
EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
URL_QUERY_RE = re.compile(r"(?P<path>/[^\s\"'?]+)\?(?P<query>[^\s\"']+)")


def _redact_query(match: re.Match[str]) -> str:
    keys = []
    for part in match.group("query").split("&"):
        key = part.partition("=")[0]
        if key and key not in keys:
            keys.append(key)
    suffix = "&".join(f"{key}=<redacted>" for key in keys)
    return f"{match.group('path')}?{suffix}" if suffix else match.group("path")


def _redact_ipv6(match: re.Match[str]) -> str:
    try:
        ipaddress.IPv6Address(match.group(0))
    except ipaddress.AddressValueError:
        return match.group(0)
    return "<ip-redacted>"


def sanitize_evidence(text: str) -> str:
    """Remove credentials and unnecessary identities while retaining evidence."""
    safe = PRIVATE_KEY_RE.sub("<private-key-redacted>", text or "")
    safe = STANDALONE_SECRET_RE.sub("<secret-redacted>", safe)
    safe = JWT_RE.sub("<jwt-redacted>", safe)
    safe = BEARER_RE.sub(r"\1<redacted>", safe)
    safe = SECRET_HEADER_RE.sub(r"\1<redacted>", safe)
    safe = SECRET_FIELD_RE.sub(r"\1<redacted>", safe)
    safe = REQUEST_BODY_RE.sub(r"\1<redacted>", safe)
    safe = URL_CREDENTIAL_RE.sub(r"\1<redacted>\3", safe)
    safe = URL_QUERY_RE.sub(_redact_query, safe)
    safe = UUID_RE.sub("<id-redacted>", safe)
    safe = EMAIL_RE.sub("<email-redacted>", safe)
    safe = IPV4_RE.sub("<ip-redacted>", safe)
    return IPV6_RE.sub(_redact_ipv6, safe)


def sanitize_artifact(text: str) -> str:
    """Apply a last redaction pass to text leaving the investigation boundary."""
    return sanitize_evidence(text)


def canonical_evidence(text: str) -> str:
    """The stable incident shape used for deduplication.

    Query parameter names remain useful when two request contracts differ,
    but their values do not.  Nginx connection ids, byte counts, timestamps
    and other numbers are removed by the fingerprint function afterwards.
    """
    return sanitize_evidence(text)
