from maajun.config import RepoConfig
from maajun.daemon.reports import artifact_title, issue_body, verification_section
from maajun.daemon.verification import VerificationCheck, VerificationSummary
from maajun.monitors import ErrorEvent, fingerprint
from maajun.privacy import sanitize_artifact, sanitize_evidence
from maajun.vcs import CommandResult

SYNTHETIC_PROVIDER_TOKEN = "sk-" + ("x" * 32)
SYNTHETIC_API_KEY = "key-" + ("x" * 24)
SYNTHETIC_PASSWORD = "value-" + ("x" * 24)


def test_runtime_evidence_is_redacted_before_any_consumer_sees_it():
    raw = (
        'client: 203.0.113.8, request: "POST /upload?bot_id='
        '33c2d384-faec-4e09-97d4-e588bd89ab2e HTTP/1.1", '
        'Authorization: Bearer eyJabc.def.ghi, user@example.com'
    )

    event = ErrorEvent(source="logfile:/tmp/error.log", message=raw, details=raw)

    assert "203.0.113.8" not in event.details
    assert "33c2d384-faec-4e09-97d4-e588bd89ab2e" not in event.details
    assert "eyJabc.def.ghi" not in event.details
    assert "user@example.com" not in event.details
    assert "bot_id=<redacted>" in event.details
    assert event.message == event.details


def test_ipv6_addresses_are_redacted_without_hiding_timestamps():
    safe = sanitize_evidence("2026-09-04T12:34:56Z client=2001:db8::1")

    assert "2001:db8::1" not in safe
    assert "12:34:56" in safe


def test_query_values_and_volatile_nginx_numbers_do_not_split_one_incident():
    first = (
        '2026/09/04 [error] 100#100: *40 client intended to send too large body: '
        '5653748 bytes, client: 203.0.113.8, request: "POST /api/upload HTTP/1.1"'
    )
    second = (
        '2026/09/05 [error] 200#200: *91 client intended to send too large body: '
        '5653515 bytes, client: 198.51.100.3, request: '
        '"POST /api/upload?bot_id=33c2d384-faec-4e09-97d4-e588bd89ab2e HTTP/1.1"'
    )

    assert fingerprint(first) == fingerprint(second)


def test_sanitizer_preserves_route_and_query_key_as_diagnostic_evidence():
    safe = sanitize_evidence("POST /api/upload?workspace_id=secret&retry=2")

    assert safe == "POST /api/upload?workspace_id=<redacted>&retry=<redacted>"


def test_structured_secrets_and_request_bodies_are_removed():
    raw = (
        'headers={"Authorization": "Bearer top-secret", '
        '"Cookie": "session=private"} '
        f'payload={{"api_key": "{SYNTHETIC_API_KEY}", '
        f'"password": "{SYNTHETIC_PASSWORD}"}}\n'
        'Request body: {"medical_note": "private"}'
    )

    safe = sanitize_evidence(raw)

    assert "top-secret" not in safe
    assert "session=private" not in safe
    assert SYNTHETIC_API_KEY not in safe
    assert SYNTHETIC_PASSWORD not in safe
    assert "medical_note" not in safe


def test_standalone_provider_tokens_and_url_passwords_are_removed():
    raw = (
        f"token {SYNTHETIC_PROVIDER_TOKEN} "
        "database postgresql://service:plain-password@db.internal/app"
    )

    safe = sanitize_evidence(raw)

    assert SYNTHETIC_PROVIDER_TOKEN not in safe
    assert "plain-password" not in safe
    assert "postgresql://service:<redacted>@db.internal/app" in safe


def test_artifact_gate_rechecks_model_generated_text():
    report = (
        "## Root cause\nThe tool printed "
        f"api_key={SYNTHETIC_API_KEY} in its output."
    )

    safe = sanitize_artifact(report)

    assert SYNTHETIC_API_KEY not in safe
    assert "api_key=<redacted>" in safe


def test_github_body_and_title_redact_agent_output_again():
    event = ErrorEvent(source="journalctl:app", message="upload failed", details="500")
    report = (
        f"# API key {SYNTHETIC_PROVIDER_TOKEN} broke upload\n\n"
        f"## Root cause\nA tool exposed password={SYNTHETIC_PASSWORD}.\n\n"
        "## Suggested fix\nKeep the credential out of logs."
    )

    body = issue_body(event, report)
    title = artifact_title(report, event.message)

    assert SYNTHETIC_PROVIDER_TOKEN not in body
    assert SYNTHETIC_PASSWORD not in body
    assert SYNTHETIC_PROVIDER_TOKEN not in title


def test_verification_output_is_redacted_before_publication():
    summary = VerificationSummary(checks=(VerificationCheck(
        "pytest -q",
        CommandResult(1, "Authorization: Bearer deployment-secret"),
    ),))

    rendered = verification_section(RepoConfig(repo="owner/name"), summary)

    assert "deployment-secret" not in rendered
    assert "Authorization: <redacted>" in rendered
