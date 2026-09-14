"""Unit tests for the guardrails engine — URL allowlist, risk classification,
and PII redaction. These are pure logic (no browser), so they run fast and
are exactly the "where it counts" code the safety/escalation model leans on.
"""

from guardrails import GuardrailsEngine, RiskLevel


def test_url_outside_allowlist_is_blocked():
    g = GuardrailsEngine()
    ok, reason = g.check("https://evil.com/steal", "navigate", None, "https://evil.com")
    assert ok is False
    assert "not in allowlist" in reason


def test_blocked_path_within_allowed_origin_is_blocked():
    g = GuardrailsEngine()
    ok, reason = g.check("http://127.0.0.1:5001/admin", "navigate")
    assert ok is False
    assert "blocked" in reason.lower()


def test_safe_read_action_is_permitted():
    g = GuardrailsEngine()
    ok, reason = g.check("http://127.0.0.1:5001/dashboard", "click", "text=Search Members")
    assert ok is True
    assert reason == ""


def test_risky_selector_upgrades_click_to_risky():
    g = GuardrailsEngine()
    risk = g.classify_risk("click", "button.btn-submit-confirm", None)
    assert risk == RiskLevel.RISKY


def test_blocked_selector_pattern_is_never_permitted():
    g = GuardrailsEngine()
    ok, reason = g.check("http://127.0.0.1:5001/transfer", "click", "btn-wire-transfer")
    assert ok is False
    assert "BLOCKED" in reason


def test_navigate_is_safe_by_default():
    g = GuardrailsEngine()
    risk = g.classify_risk("navigate", None, "http://127.0.0.1:5001/dashboard")
    assert risk == RiskLevel.SAFE


def test_redact_scrubs_ssn_and_password():
    text = "Password: bank123 | SSN: 123-45-6789 | Member ID: 12345"
    redacted = GuardrailsEngine.redact(text)
    assert "bank123" not in redacted
    assert "123-45-6789" not in redacted
    assert "12345" in redacted   # not PII — must survive redaction


def test_redact_scrubs_email():
    redacted = GuardrailsEngine.redact("contact john.smith@email.com for details")
    assert "john.smith@email.com" not in redacted
    assert "[REDACTED_EMAIL]" in redacted


def test_sanitize_artifact_removes_blocked_fields_recursively():
    data = {
        "step": 1,
        "inputs": [{"name": "password", "value": "hunter2"}],
        "nested": {"ssn": "111-22-3333", "member_id": "12345"},
    }
    cleaned = GuardrailsEngine.sanitize_artifact(data)
    assert cleaned["inputs"][0]["value"] == "[REDACTED]"
    assert cleaned["nested"]["ssn"] == "[REDACTED]"
    assert cleaned["nested"]["member_id"] == "12345"


def test_sanitize_artifact_redacts_pii_inside_free_text_strings():
    data = {"notes": "operator entered ssn 123-45-6789 during handoff"}
    cleaned = GuardrailsEngine.sanitize_artifact(data)
    assert "123-45-6789" not in cleaned["notes"]
