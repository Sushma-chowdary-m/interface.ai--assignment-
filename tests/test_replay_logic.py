"""
Unit tests for replay.py's pure decision logic: business-outcome detection,
checkpoint verification, and output extraction. These are the load-bearing
pieces of the error taxonomy (Tier 1/2/3) described in REPORT.md, so they're
tested against a fake Page/Locator rather than a real browser — fast,
deterministic, and exercises the exact regex/selector logic replay uses.
"""

import pytest

from artifact import OutputExtraction, SuccessCheckpoint, ArtifactStep, LocatorStrategy
from replay import (
    _detect_business_outcome, _detect_session_expired,
    _verify_checkpoints, _extract_output,
)


class FakeLocator:
    def __init__(self, text: str = "", attrs: dict | None = None, visible: bool = True):
        self._text = text
        self._attrs = attrs or {}
        self._visible = visible

    @property
    def first(self):
        return self

    async def inner_text(self, timeout: int | None = None):
        return self._text

    async def get_attribute(self, name: str, timeout: int | None = None):
        return self._attrs.get(name)

    async def wait_for(self, state: str = "visible", timeout: int | None = None):
        if not self._visible:
            raise TimeoutError("not visible")


class FakePage:
    def __init__(self, url: str, title: str, body_text: str, locators: dict | None = None):
        self.url = url
        self._title = title
        self._body_text = body_text
        self._locators = locators or {}

    async def title(self):
        return self._title

    async def inner_text(self, selector: str):
        return self._body_text

    def locator(self, selector: str):
        # Default: element not present (visible=False) so unmocked selectors
        # behave like "not found" and callers fall back to full-body text —
        # matching a real page with no matching container.
        return self._locators.get(selector, FakeLocator(visible=False))


# ---------------------------------------------------------------------------
# Business outcome detection (Tier 1)
# ---------------------------------------------------------------------------

async def test_member_not_found_is_detected_as_business_outcome():
    page = FakePage(
        url="http://127.0.0.1:5001/member/00000",
        title="Error",
        body_text="Member 00000 not found.",
        locators={".alert-error": FakeLocator(text="Member 00000 not found.")},
    )
    outcome = await _detect_business_outcome(page)
    assert outcome == "member_not_found"


async def test_permission_denied_is_detected():
    # "permission denied" matches before "restricted" (first-match-wins,
    # patterns are ordered most-specific-first) — both labels are valid
    # for this message; access_denied is the one that actually fires.
    page = FakePage(
        url="http://127.0.0.1:5001/member/99999",
        title="Error",
        body_text="Permission denied. This account is restricted.",
        locators={".alert-error": FakeLocator(text="Permission denied. This account is restricted.")},
    )
    outcome = await _detect_business_outcome(page)
    assert outcome == "access_denied"


async def test_restricted_without_denial_wording_is_still_detected():
    text = "This account is currently restricted pending review."
    page = FakePage(
        url="http://127.0.0.1:5001/member/99999",
        title="Error",
        body_text=text,
        locators={".alert-error": FakeLocator(text=text)},
    )
    outcome = await _detect_business_outcome(page)
    assert outcome == "member_restricted"


async def test_session_expired_is_detected_separately_from_business_outcomes():
    page = FakePage(
        url="http://127.0.0.1:5001/login",
        title="FirstBank Enterprise Portal",
        body_text="Session expired. Please log in again.",
        locators={".alert-error": FakeLocator(text="Session expired. Please log in again.")},
    )
    assert await _detect_session_expired(page) is True
    # And it must NOT be misreported as a generic business outcome —
    # the caller needs to re-authenticate, not treat it as a valid answer.
    assert await _detect_business_outcome(page) is None


async def test_normal_page_has_no_business_outcome():
    page = FakePage(
        url="http://127.0.0.1:5001/member/12345",
        title="Member Profile",
        body_text="Savings balance: $4,250.00",
    )
    outcome = await _detect_business_outcome(page)
    assert outcome is None


async def test_static_reference_text_elsewhere_on_page_is_not_a_false_positive():
    # Regression test: the real mock bank's search page renders a static
    # "Test Member IDs" help table containing the literal words "Permission
    # Denied" and "Not Found" as documentation — always present, regardless
    # of the actual search result. An earlier version of this detector
    # scanned the *whole* page body and false-positived on that table on
    # every single replay, including fully successful ones. Fix: detection
    # only trusts the scoped `.alert-error` container; when it's absent (a
    # successful search has no error box) there is no fallback scan of the
    # rest of the page, so this reference text can never trigger a
    # business outcome no matter what it says.
    page = FakePage(
        url="http://127.0.0.1:5001/search",
        title="Member Search",
        body_text=(
            "Search Member\n"
            "Test Member IDs\n"
            "12345 — Active Member\n"
            "67890 — Active Member\n"
            "99999 — Restricted User — Permission Denied\n"
            "00000 — Not Found\n"
        ),
        # No .alert-error container present — this is the successful-search case.
    )
    outcome = await _detect_business_outcome(page)
    assert outcome is None


async def test_scoped_error_container_is_used_when_present():
    error_box = FakeLocator(text="Permission denied. This account is restricted.")
    page = FakePage(
        url="http://127.0.0.1:5001/member/99999",
        title="System Error",
        body_text=(
            "Test Member IDs\n99999 — Restricted User — Permission Denied\n"
            "Permission denied. This account is restricted."
        ),
        locators={".alert-error": error_box},
    )
    outcome = await _detect_business_outcome(page)
    assert outcome == "access_denied"


# ---------------------------------------------------------------------------
# Checkpoint verification
# ---------------------------------------------------------------------------

def _step_with_checkpoints(checkpoints):
    return ArtifactStep(
        step_number=1, action_kind="click", description="test",
        locator=None, inputs=[], outputs=[], checkpoints=checkpoints,
        risk_level="safe", is_reversible=True,
    )


async def test_url_contains_checkpoint_passes_when_present():
    page = FakePage(url="http://127.0.0.1:5001/member/12345", title="x", body_text="")
    step = _step_with_checkpoints([SuccessCheckpoint(kind="url_contains", value="/member/12345")])
    assert await _verify_checkpoints(page, step) is True


async def test_url_contains_checkpoint_fails_when_absent():
    page = FakePage(url="http://127.0.0.1:5001/dashboard", title="x", body_text="")
    step = _step_with_checkpoints([SuccessCheckpoint(kind="url_contains", value="/member/12345")])
    assert await _verify_checkpoints(page, step) is False


async def test_title_contains_checkpoint_is_case_insensitive():
    page = FakePage(url="http://x", title="FirstBank Enterprise Portal", body_text="")
    step = _step_with_checkpoints([SuccessCheckpoint(kind="title_contains", value="firstbank")])
    assert await _verify_checkpoints(page, step) is True


async def test_all_checkpoints_must_pass():
    page = FakePage(url="http://127.0.0.1:5001/member/12345", title="Wrong Title", body_text="")
    step = _step_with_checkpoints([
        SuccessCheckpoint(kind="url_contains", value="/member/12345"),
        SuccessCheckpoint(kind="title_contains", value="Member Profile"),
    ])
    assert await _verify_checkpoints(page, step) is False


# ---------------------------------------------------------------------------
# Output extraction
# ---------------------------------------------------------------------------

async def test_extract_text_with_regex_pattern():
    page = FakePage(
        url="http://x", title="x", body_text="",
        locators={".balance-amount": FakeLocator(text="Balance: $4,250.00 available")},
    )
    rule = OutputExtraction(
        field_name="savings_balance", source="text",
        selector=".balance-amount", regex_pattern=r"\$[\d,]+\.\d{2}",
    )
    value = await _extract_output(page, rule)
    assert value == "$4,250.00"


async def test_extract_attribute():
    page = FakePage(
        url="http://x", title="x", body_text="",
        locators={"#acct-num": FakeLocator(attrs={"data-account": "SAV-12345-NEW"})},
    )
    rule = OutputExtraction(
        field_name="account_number", source="attribute",
        selector="#acct-num", attribute="data-account",
    )
    value = await _extract_output(page, rule)
    assert value == "SAV-12345-NEW"


async def test_extract_missing_element_returns_none_not_exception():
    page = FakePage(url="http://x", title="x", body_text="")

    class BrokenLocator(FakeLocator):
        async def inner_text(self, timeout=None):
            raise TimeoutError("element vanished")

    page._locators = {".missing": BrokenLocator()}
    rule = OutputExtraction(field_name="x", source="text", selector=".missing")
    value = await _extract_output(page, rule)
    assert value is None
