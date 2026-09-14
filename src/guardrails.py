"""
src/guardrails.py
-----------------
Safety guardrails for the banking agent.

Three layers:
  1. URL allowlist   — agent may only visit approved origins/paths
  2. Action policy   — safe vs. risky action classification with confirmation gate
  3. PII redaction   — scrub logs and artifacts of credentials / member data

Everything is configuration-driven so operators can adjust without code changes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Risk tiers
# ---------------------------------------------------------------------------

class RiskLevel(str, Enum):
    SAFE        = "safe"        # read-only, fully reversible
    CAUTION     = "caution"     # writes but reversible (e.g., open draft)
    RISKY       = "risky"       # irreversible write (open account, transfer)
    BLOCKED     = "blocked"     # never permitted


# ---------------------------------------------------------------------------
# URL allowlist
# ---------------------------------------------------------------------------

# Only paths rooted at the mock-bank portal are permitted.
# Regex patterns matched against the full URL.
ALLOWED_URL_PATTERNS: list[str] = [
    r"^http://127\.0\.0\.1:5001",   # entire mock bank
    r"^http://localhost:5001",
]

# Paths the agent should NEVER navigate to even within allowed origins.
BLOCKED_PATH_PATTERNS: list[str] = [
    r"/admin",
    r"/debug",
    r"/internal",
]


# ---------------------------------------------------------------------------
# Action risk classification
# ---------------------------------------------------------------------------

# Maps action kinds → risk level
ACTION_RISK: dict[str, RiskLevel] = {
    "navigate":   RiskLevel.SAFE,
    "screenshot": RiskLevel.SAFE,
    "wait":       RiskLevel.SAFE,
    "click":      RiskLevel.CAUTION,   # re-evaluated below by selector heuristics
    "type":       RiskLevel.CAUTION,
    "select":     RiskLevel.CAUTION,
    "done":       RiskLevel.SAFE,
    "escalate":   RiskLevel.SAFE,
}

# Patterns that indicate a risky / irreversible action. Matched against BOTH
# the selector/value AND the action's human-readable description (see
# classify_risk) — deliberately does NOT include a bare "submit": almost
# every HTML form button is `type="submit"` (login, search, anything), so
# matching that substring flags nearly all form interactions as risky
# regardless of what the form actually does. Caught by testing against the
# real artifacts: "Sign In", "SEARCH", and the actual risky "REVIEW &
# CONFIRM new account" button all resolve to the identical CSS selector
# `button[type='submit']` — the selector alone cannot distinguish them.
# Matching "confirm" against the description text (which the safe/caution
# selector-only version couldn't see) is what correctly separates them.
RISKY_SELECTOR_PATTERNS: list[str] = [
    r"confirm",
    r"open.account",
    r"transfer",
    r"delete",
    r"close.account",
    r"btn-danger",
]

# Selectors that are always blocked
BLOCKED_SELECTOR_PATTERNS: list[str] = [
    r"wire.transfer",
    r"bulk.transfer",
    r"admin",
]


# ---------------------------------------------------------------------------
# PII / credential patterns for redaction
# ---------------------------------------------------------------------------

_PII_PATTERNS: list[tuple[str, str]] = [
    # Labeled password occurrences in free-text log lines (e.g. a raw
    # exception message or human handoff note that happens to include
    # "password: <value>"). Deliberately NOT a hardcoded literal tied to
    # one specific demo credential — a redaction rule that only matches
    # today's password stops working the moment that password rotates,
    # which defeats the point. The structural defense for the common case
    # (a credential captured as a named input/output field, e.g.
    # {"name": "password", "value": "..."}) lives in sanitize_artifact()
    # below and doesn't depend on knowing the value in advance at all —
    # this pattern is the secondary net for the same secret leaking into
    # unstructured text instead.
    (r"(?i)\b(password|passwd|pwd)\b\s*[:=]\s*\S+", "[REDACTED_PASSWORD]"),
    # US SSN
    (r"\b\d{3}-\d{2}-\d{4}\b", "[REDACTED_SSN]"),
    # US credit card (basic Luhn-structure check omitted — pattern match only)
    (r"\b(?:\d{4}[- ]){3}\d{4}\b", "[REDACTED_CARD]"),
    # Routing numbers (9 digits)
    (r"\b\d{9}\b", "[REDACTED_ROUTING]"),
    # Email addresses in logs
    (r"[\w.+-]+@[\w-]+\.[a-z]{2,}", "[REDACTED_EMAIL]"),
]

_PII_COMPILED = [(re.compile(p, re.IGNORECASE), r) for p, r in _PII_PATTERNS]

# Fields that must never appear in artifacts even if agent extracts them
BLOCKED_ARTIFACT_FIELDS: set[str] = {
    "password", "ssn", "pin", "routing_number", "card_number", "secret",
}


# ---------------------------------------------------------------------------
# Guardrails engine
# ---------------------------------------------------------------------------

@dataclass
class GuardrailsEngine:
    """
    Stateless guardrails evaluator.

    call check() before every agent action.
    call redact() on any string before logging or persisting.
    call sanitize_artifact() before writing a JSON artifact.
    """

    # Operators can override these at construction time
    allowed_url_patterns:     list[str] = field(default_factory=lambda: list(ALLOWED_URL_PATTERNS))
    blocked_path_patterns:    list[str] = field(default_factory=lambda: list(BLOCKED_PATH_PATTERNS))
    risky_selector_patterns:  list[str] = field(default_factory=lambda: list(RISKY_SELECTOR_PATTERNS))
    blocked_selector_patterns: list[str] = field(default_factory=lambda: list(BLOCKED_SELECTOR_PATTERNS))
    require_confirmation_for_risky: bool = True

    def __post_init__(self):
        self._url_allow    = [re.compile(p) for p in self.allowed_url_patterns]
        self._path_block   = [re.compile(p, re.IGNORECASE) for p in self.blocked_path_patterns]
        self._sel_risky    = [re.compile(p, re.IGNORECASE) for p in self.risky_selector_patterns]
        self._sel_blocked  = [re.compile(p, re.IGNORECASE) for p in self.blocked_selector_patterns]

    # ------------------------------------------------------------------
    # URL checks
    # ------------------------------------------------------------------

    def _url_allowed(self, url: str) -> tuple[bool, str]:
        if not any(p.match(url) for p in self._url_allow):
            return False, f"URL not in allowlist: {url}"
        if any(p.search(url) for p in self._path_block):
            return False, f"URL path is blocked: {url}"
        return True, ""

    # ------------------------------------------------------------------
    # Action risk classification
    # ------------------------------------------------------------------

    def classify_risk(
        self,
        action_kind: str,
        selector: str | None,
        value: str | None,
        description: str | None = None,
    ) -> RiskLevel:
        """
        `description` (the action's human-readable intent, e.g. "Click the
        REVIEW & CONFIRM button") matters because selectors for generic form
        controls are often indistinguishable — a login button, a search
        button, and an irreversible "confirm new account" button can all
        resolve to the identical `button[type='submit']` CSS selector. The
        selector alone cannot separate them; the description usually can.
        """
        sel  = (selector or "").lower()
        val  = (value or "").lower()
        desc = (description or "").lower()

        # Blocked selector?
        if any(p.search(sel) for p in self._sel_blocked):
            return RiskLevel.BLOCKED

        # Start from base risk
        risk = ACTION_RISK.get(action_kind, RiskLevel.CAUTION)

        # Upgrade to RISKY if selector, value, or description matches a
        # risky pattern.
        if risk == RiskLevel.CAUTION:
            if any(p.search(sel) for p in self._sel_risky):
                risk = RiskLevel.RISKY
            if any(p.search(val) for p in self._sel_risky):
                risk = RiskLevel.RISKY
            if any(p.search(desc) for p in self._sel_risky):
                risk = RiskLevel.RISKY

        return risk

    # ------------------------------------------------------------------
    # Main gate
    # ------------------------------------------------------------------

    def check(
        self,
        url:         str,
        action_kind: str,
        selector:    str | None = None,
        value:       str | None = None,
        description: str | None = None,
    ) -> tuple[bool, str]:
        """
        Return (permitted: bool, reason: str).
        permitted=False means the action must be blocked.
        """
        # 1. URL allowlist
        ok, reason = self._url_allowed(url)
        if not ok:
            return False, reason

        # 2. Classify risk
        risk = self.classify_risk(action_kind, selector, value, description)

        if risk == RiskLevel.BLOCKED:
            return False, f"Action classified as BLOCKED: {action_kind} / {selector}"

        # RISKY actions are permitted by the agent but flagged in the log.
        # The require_confirmation_for_risky flag is checked by the *orchestrator*
        # (replay.py) — not here, since the discovery run must be allowed through.
        # In replay, RISKY steps trigger a confirmation gate.

        return True, ""

    def is_risky(
        self, action_kind: str, selector: str | None, value: str | None,
        description: str | None = None,
    ) -> bool:
        return self.classify_risk(action_kind, selector, value, description) == RiskLevel.RISKY

    # ------------------------------------------------------------------
    # PII redaction
    # ------------------------------------------------------------------

    @staticmethod
    def redact(text: str) -> str:
        """Remove known PII patterns from a string."""
        for pattern, replacement in _PII_COMPILED:
            text = pattern.sub(replacement, text)
        return text

    @staticmethod
    def sanitize_artifact(data: Any) -> Any:
        """
        Recursively walk a JSON-serialisable structure and:
          - Remove blocked field names (e.g. a top-level "password" key)
          - Remove the *value* half of a {"name": "password", "value": ...}
            pair — this is how InputParameter/OutputExtraction actually
            serialize (a generic name/value shape), so a key-only check
            would miss a captured password sitting under the literal key
            "value" whose sibling "name" is "password". Both shapes must be
            covered or a typed-into-a-password-field step leaks the
            plaintext value into the artifact JSON.
          - Redact string values that match PII patterns
        """
        if isinstance(data, dict):
            is_blocked_name_value_pair = (
                "name" in data
                and "value" in data
                and isinstance(data.get("name"), str)
                and data["name"].lower() in BLOCKED_ARTIFACT_FIELDS
            )
            cleaned = {}
            for k, v in data.items():
                if k.lower() in BLOCKED_ARTIFACT_FIELDS:
                    cleaned[k] = "[REDACTED]"
                elif k == "value" and is_blocked_name_value_pair:
                    cleaned[k] = "[REDACTED]"
                else:
                    cleaned[k] = GuardrailsEngine.sanitize_artifact(v)
            return cleaned
        if isinstance(data, list):
            return [GuardrailsEngine.sanitize_artifact(item) for item in data]
        if isinstance(data, str):
            return GuardrailsEngine.redact(data)
        return data


# ---------------------------------------------------------------------------
# Convenience type alias used by other modules
# ---------------------------------------------------------------------------

ActionType = str   # "navigate" | "click" | "type" | "select" | "wait" | "done" | "escalate"


# ---------------------------------------------------------------------------
# CLI self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    g = GuardrailsEngine()

    tests = [
        ("http://127.0.0.1:5001/dashboard", "click", "text=Search Members", None),
        ("http://127.0.0.1:5001/open_account", "click", "btn-submit-confirm", None),
        ("https://evil.com/steal", "navigate", None, "https://evil.com"),
        ("http://127.0.0.1:5001/admin", "navigate", None, None),
    ]

    for url, kind, sel, val in tests:
        ok, reason = g.check(url, kind, sel, val)
        risk = g.classify_risk(kind, sel, val)
        print(f"[{'OK' if ok else 'BLOCK'}] {kind:10s} {(sel or val or '')[:40]:40s} risk={risk.value:8s} {reason}")

    print("\nPII redaction test:")
    sample = "Password: REDACTED_PASSWORD | SSN: 123-45-6789 | Member ID: 482915"
    print(f"  Before: {sample}")
    print(f"  After:  {g.redact(sample)}")