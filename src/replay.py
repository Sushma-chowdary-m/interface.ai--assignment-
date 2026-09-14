"""
src/replay.py
-------------
Deterministic Replay Engine — executes a saved Artifact WITHOUT Claude.

Error tier hierarchy:
  Tier 1 — Business outcome   (member not found, account already exists)
            → Log as INFO, return structured result, do NOT crash
  Tier 2 — Recoverable        (timeout, stale element, dismissible dialog)
            → Retry up to step.retry_count, dismiss dialogs, continue
  Tier 3 — Hard failure       (element never found, unexpected page, crash)
            → Stop immediately, save debuggable evidence, raise ReplayError

RISKY steps (risk_level == "risky") require explicit operator confirmation
before execution. Set require_confirmation=False to auto-approve (CI mode).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PWTimeout,
    async_playwright,
)

from dotenv import load_dotenv

# Must run before the BANK_USERNAME/BANK_PASSWORD reads below. Safe to call
# even when a caller (run_tasks.py, api.py) already loaded .env earlier —
# load_dotenv() is a no-op if the vars are already set — but required here
# too since running `python src/replay.py` directly makes this the entry
# point, and module-level code above runs before any `if __name__` guard.
load_dotenv()

from artifact import Artifact, ArtifactStep, InputParameter, OutputExtraction
from guardrails import GuardrailsEngine
from logger import ReplayLogger

EVIDENCE_DIR  = Path(__file__).parent.parent / "evidence"
ARTIFACTS_DIR = Path(__file__).parent.parent / "artifacts"

# Read from env only — never hardcoded, never written into the artifact.
# This is a known simplification (see REPORT.md "Cuts"): the mock portal's
# login is engine-level glue rather than an artifact-declared step, because
# auth flows are usually app-wide rather than per-capability. A production
# version would model this as a reusable "authenticate" artifact/step type
# that is composed in front of any capability for a given tenant, with the
# credential *reference* (not value) declared in the artifact and the value
# resolved from a secrets manager at replay time.
BANK_USERNAME = os.getenv("BANK_USERNAME", "")
BANK_PASSWORD = os.getenv("BANK_PASSWORD", "")

# ---------------------------------------------------------------------------
# Error types
# ---------------------------------------------------------------------------

class BusinessOutcome(Exception):
    """Tier 1 — expected non-happy-path result (e.g. member not found)."""
    def __init__(self, reason: str, data: dict | None = None):
        super().__init__(reason)
        self.reason = reason
        self.data   = data or {}

class RecoverableError(Exception):
    """Tier 2 — transient failure that retry / dialog-dismiss can fix."""


class SessionExpiredError(RecoverableError):
    """
    Tier 2 (specialized) — the app kicked us back to a login screen mid-flow.
    Handled differently from a generic RecoverableError: the retry loop
    re-authenticates before re-attempting the step, rather than just
    waiting and retrying against a page that will never succeed.
    """

class ReplayError(Exception):
    """Tier 3 — hard failure. Replay stops and surfaces this for debugging."""
    def __init__(self, reason: str, step_number: int, screenshot_path: str | None = None):
        super().__init__(reason)
        self.reason          = reason
        self.step_number     = step_number
        self.screenshot_path = screenshot_path


# ---------------------------------------------------------------------------
# Business-outcome detectors
# ---------------------------------------------------------------------------

# Ordered — first match wins. Patterns are matched against the actual
# rendered page text, so they're derived from the target app's real copy
# (see mock_bank/app.py and templates/error.html), not guessed generically.
# This matters: a plausible-looking pattern that doesn't match the real
# strings is silently dead code — exactly what shipped here originally
# (e.g. "member not found" never matches "No member found with ID: 482915"
# or "Member 482915 not found."). Verified against every error string the
# mock app actually renders; extend this list per-target when reusing the
# engine against a different app (see REPORT.md "Heterogeneity").
BUSINESS_OUTCOME_PATTERNS: list[tuple[str, str]] = [
    (r"no member found",                 "member_not_found"),
    (r"member\s+\S+\s+not found",        "member_not_found"),
    (r"permission denied",               "access_denied"),
    (r"restricted",                      "member_restricted"),
    (r"account.*already exists",         "account_already_exists"),
    (r"error.*404",                      "not_found"),
]

_BO_COMPILED = [(re.compile(p, re.IGNORECASE), r) for p, r in BUSINESS_OUTCOME_PATTERNS]

# Checked separately (see SessionExpiredError) because it needs a different
# response — re-authenticate and retry — not "return this as a valid answer".
_SESSION_EXPIRED_PATTERN = re.compile(r"session\s+(has\s+)?expired", re.IGNORECASE)


# The app renders EVERY real error/business-outcome (member not found,
# permission denied, session expired) inside one dedicated container,
# `.alert-error` — verified against login.html, search.html, and
# error.html. Scanning the *whole* page body instead is a trap: this app's
# search page also renders a static "Test Member IDs" reference table
# containing the literal words "Permission Denied" and "Not Found" as
# documentation for testers, always present regardless of the actual
# search result. An earlier version of this detector scanned full body
# text and false-positived on that reference table on every single replay
# — including fully successful ones — caught by inspecting a real replay
# run's evidence log, not by code review (see tests/test_replay_logic.py
# for the regression test). Because every real error in this app goes
# through `.alert-error`, absence of that container means "no error" —
# there is deliberately no full-body fallback, since a fallback here would
# reintroduce the exact false-positive it was meant to catch. A different
# target app would declare its own container selector (per-tenant config,
# not hardcoded), following the same principle: detect against a scoped,
# known-error-only region, never the whole rendered page.
_ERROR_CONTAINER_SELECTOR = ".alert-error"


async def _error_container_text(page: Page) -> str | None:
    """Text of the scoped error region, or None if it isn't present."""
    try:
        loc = page.locator(_ERROR_CONTAINER_SELECTOR).first
        await loc.wait_for(state="visible", timeout=500)
        return await loc.inner_text()
    except Exception:
        return None


async def _detect_business_outcome(page: Page) -> str | None:
    """Return a reason string if the page shows a known business outcome."""
    scoped = await _error_container_text(page)
    if scoped is None:
        return None
    try:
        title = (await page.title()).lower()
        combined = scoped.lower() + " " + title
        for pattern, reason in _BO_COMPILED:
            if pattern.search(combined):
                return reason
    except Exception:
        pass
    return None


async def _detect_session_expired(page: Page) -> bool:
    scoped = await _error_container_text(page)
    if scoped is None:
        return False
    return bool(_SESSION_EXPIRED_PATTERN.search(scoped))


# ---------------------------------------------------------------------------
# Checkpoint verification
# ---------------------------------------------------------------------------

async def _verify_checkpoints(page: Page, step: ArtifactStep) -> bool:
    """Return True if ALL checkpoints for this step pass."""
    for chk in step.checkpoints:
        try:
            if chk.kind == "url_contains":
                if chk.value not in page.url:
                    return False

            elif chk.kind == "title_contains":
                title = await page.title()
                if chk.value.lower() not in title.lower():
                    return False

            elif chk.kind == "element_visible":
                loc = page.locator(chk.selector or chk.value)
                await loc.wait_for(state="visible", timeout=3_000)

            elif chk.kind == "element_text":
                loc = page.locator(chk.selector or "body")
                text = await loc.inner_text()
                if chk.value.lower() not in text.lower():
                    return False

        except Exception:
            return False
    return True


# ---------------------------------------------------------------------------
# Output extraction
# ---------------------------------------------------------------------------

async def _extract_output(page: Page, rule: OutputExtraction) -> str | None:
    """Apply one OutputExtraction rule to the live page."""
    try:
        if rule.source == "url":
            return page.url
        if rule.source == "title":
            return await page.title()

        if rule.selector:
            loc = page.locator(rule.selector).first

            if rule.source == "text":
                text = await loc.inner_text(timeout=5_000)
                if rule.regex_pattern:
                    m = re.search(rule.regex_pattern, text, re.IGNORECASE)
                    return m.group(0) if m else text.strip()
                return text.strip()

            if rule.source == "attribute" and rule.attribute:
                return await loc.get_attribute(rule.attribute, timeout=5_000)

            if rule.source == "regex":
                text = await loc.inner_text(timeout=5_000)
                m = re.search(rule.regex_pattern or "", text, re.IGNORECASE)
                return m.group(1) if m and m.lastindex else (m.group(0) if m else None)

    except Exception:
        return None
    return None


# ---------------------------------------------------------------------------
# Element interaction (multi-strategy)
# ---------------------------------------------------------------------------

async def _locate(page: Page, step: ArtifactStep, timeout_ms: int):
    """
    Try the primary locator, then fallbacks.
    Returns a Playwright Locator or raises RecoverableError.
    """
    if not step.locator:
        raise RecoverableError(f"Step {step.step_number}: no locator defined")

    candidates = [step.locator.primary] + (step.locator.fallbacks or [])

    def _get_loc(sel: str):
        if sel.startswith("role=button:"):
            # Scoped to actual <button>/<input type=submit> elements, not
            # any element containing the text. Needed because a plain
            # text= match is ambiguous whenever a nav link's label happens
            # to contain the button's label as a substring — e.g. a
            # "Search" submit button vs. a "Search Member" nav link, where
            # `text=Search` (case-insensitive substring) matches both, and
            # `.first` silently grabs whichever renders first in the DOM.
            # Found by replaying an artifact and watching it click the nav
            # link instead of submitting the form — not a hypothetical.
            return page.get_by_role("button", name=sel[len("role=button:"):], exact=False).first
        elif sel.startswith("text="):
            return page.get_by_text(sel[5:], exact=False).first
        elif sel.startswith("placeholder="):
            return page.get_by_placeholder(sel[12:]).first
        else:
            return page.locator(sel).first

    for sel in candidates:
        try:
            loc = _get_loc(sel)
            await loc.wait_for(state="visible", timeout=min(timeout_ms, 5_000))
            return loc
        except Exception:
            continue

    raise RecoverableError(
        f"Step {step.step_number}: element not found with any locator strategy. "
        f"Tried: {candidates}. Current URL: {page.url}"
    )
   
# ---------------------------------------------------------------------------
# Single step executor
# ---------------------------------------------------------------------------

async def _execute_step(
    page: Page,
    step: ArtifactStep,
    params: dict[str, str],
    logger: ReplayLogger,
) -> dict[str, Any]:
    """
    Execute one step, return extracted output data.
    Raises BusinessOutcome | RecoverableError | ReplayError as appropriate.
    """
    kind = step.action_kind

    def resolve_input(inp: InputParameter) -> str:
        """
        Resolve one recorded InputParameter to the value replay should
        actually use. For a templated input (is_templated=True), that's the
        caller-supplied value keyed by the parameter's logical `name` — NOT
        the discovery-time literal in `.value`. `.value` is preserved for
        audit/human-review only.

        This was previously implemented as a string-replace looking for a
        literal "{{name}}" placeholder *inside* `.value` — but `.value`
        holds the actual discovery-time value ("482915"), never the
        placeholder text, so that replace was always a silent no-op.
        Parameterized replay was therefore completely non-functional: every
        replay repeated the exact value recorded during discovery
        regardless of what params the caller passed. Caught by manually
        replaying with a different member_id and getting the *original*
        member's data back unchanged — a determinism/correctness bug, not
        just a missed error case, and the kind of thing that only surfaces
        by actually running the documented command, not by reading the code.
        """
        if inp.is_templated and inp.name in params:
            return params[inp.name]
        return inp.value

    # -------------------------------------------------------------------
    if kind == "navigate":
        # Deliberately does NOT fall back to step.page_url when there's no
        # recorded input value. page_url is documented elsewhere (see
        # ArtifactStep) as discovery-run provenance ONLY — "what page was
        # this step recorded on" — never a replay instruction. Reusing it
        # as a navigation target used to be exactly what happened here,
        # and it silently teleported replay back to the discovery-time
        # record on any navigate step whose actual value wasn't captured
        # (e.g. a scroll action) — destroying parameterization for every
        # step after it. If there's truly no target, the only safe action
        # is to stay on the current page, not guess one.
        target = resolve_input(step.inputs[0]) if step.inputs else None
        if target:
            try:
                if target.startswith("javascript:"):
                    await page.evaluate(target[len("javascript:"):])
                else:
                    await page.goto(target, wait_until="domcontentloaded", timeout=step.timeout_ms)
            except PWTimeout:
                raise RecoverableError(f"Navigation timeout to {target}")
            await asyncio.sleep(0.5)
        # else: no target was recorded — stay on the current page rather
        # than guess one; falls through to the shared outcome/extraction
        # checks below like any other step.

    # -------------------------------------------------------------------
    elif kind == "click":
        loc = await _locate(page, step, step.timeout_ms)
        try:
            await loc.click(timeout=step.timeout_ms)
        except PWTimeout:
            raise RecoverableError(f"Click timeout on {step.locator.primary if step.locator else '?'}")
        await asyncio.sleep(1.0)
    # -------------------------------------------------------------------
    elif kind == "type":
        loc = await _locate(page, step, step.timeout_ms)
        value = resolve_input(step.inputs[0]) if step.inputs else ""
        try:
            await loc.clear()
            await loc.fill(value)
        except PWTimeout:
            raise RecoverableError(f"Type timeout on {step.locator.primary if step.locator else '?'}")
        await asyncio.sleep(0.3)

    # -------------------------------------------------------------------
    elif kind == "select":
            value = resolve_input(step.inputs[0]) if step.inputs else "savings"
            # Try each selector directly without going through _locate
            selected = False
            for sel in ["select[name='account_type']", "#account_type", "select"]:
                try:
                    loc = page.locator(sel).first
                    await loc.wait_for(state="visible", timeout=3_000)
                    try:
                        await loc.select_option(value=value, timeout=5_000)
                    except Exception:
                        await loc.select_option(label=value, timeout=5_000)
                    selected = True
                    break
                except Exception:
                    continue
            if not selected:
                raise RecoverableError(f"Could not find or interact with select element")
            await asyncio.sleep(0.3)
            # Skip the normal flow below
            if await _detect_session_expired(page):
                raise SessionExpiredError(f"Step {step.step_number}: session expired")
            outcome = await _detect_business_outcome(page)
            if outcome:
                raise BusinessOutcome(outcome, {"page_url": page.url})
            extracted: dict[str, Any] = {}
            for rule in step.outputs:
                val = await _extract_output(page, rule)
                if val is not None:
                    extracted[rule.field_name] = val
            return extracted

    # -------------------------------------------------------------------
    elif kind == "wait":
        secs = float(step.inputs[0].value) if step.inputs else 1.0
        await asyncio.sleep(min(secs, 5.0))

    # -------------------------------------------------------------------
    elif kind in ("done", "screenshot"):
        pass   # handled by caller

    # Session expiry is checked before business outcomes because it needs a
    # different response (re-authenticate + retry, not "return as an answer").
    if await _detect_session_expired(page):
        raise SessionExpiredError(f"Step {step.step_number}: session expired")

    # Check for business outcomes after action
    outcome = await _detect_business_outcome(page)
    if outcome:
        raise BusinessOutcome(outcome, {"page_url": page.url, "page_title": await page.title()})

    # Extract outputs
    extracted: dict[str, Any] = {}
    for rule in step.outputs:
        val = await _extract_output(page, rule)
        if val is not None:
            extracted[rule.field_name] = val

    return extracted


# ---------------------------------------------------------------------------
# Replay result
# ---------------------------------------------------------------------------

@dataclass
class ReplayResult:
    artifact_id:    str
    task_id:        str
    success:        bool
    output:         dict[str, Any]
    steps_run:      int
    total_time_ms:  int
    stop_reason:    str    # "complete" | "business_outcome" | "hard_failure" | "escalated"
    business_outcome: str | None = None
    error:          str | None   = None
    error_step:     int | None   = None
    screenshot_path: str | None  = None


# ---------------------------------------------------------------------------
# Main replay runner
# ---------------------------------------------------------------------------

class ReplayEngine:
    """
    Replays a saved Artifact deterministically.

    async with ReplayEngine() as engine:
        result = await engine.run(artifact, params={"member_id": "482915"})
    """

    def __init__(
        self,
        headless: bool = True,
        require_confirmation: bool = True,
        run_id: str | None = None,
    ):
        self.headless             = headless
        self.require_confirmation = require_confirmation
        self.run_id               = run_id or f"replay_{int(time.time())}"
        self.guardrails           = GuardrailsEngine()
        self.logger               = ReplayLogger(run_id=self.run_id, evidence_dir=EVIDENCE_DIR)
        self._playwright: Playwright | None = None
        self._browser:    Browser | None    = None
        self._context:    BrowserContext | None = None
        self.page:        Page | None       = None

    async def __aenter__(self) -> "ReplayEngine":
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless, args=["--no-sandbox"]
        )
        self._context = await self._browser.new_context(
            viewport={"width": 1280, "height": 800}, locale="en-US"
        )
        self.page = await self._context.new_page()
        self.logger.info("ReplayEngine browser launched", {"run_id": self.run_id})
        return self

    async def __aexit__(self, *_) -> None:
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    # ------------------------------------------------------------------
    # Confirmation gate for risky steps
    # ------------------------------------------------------------------

    async def _confirm_risky_step(self, step: ArtifactStep) -> bool:
        """
        Prompt operator to approve a RISKY step.
        In CI (require_confirmation=False) auto-approve.
        """
        if not self.require_confirmation:
            self.logger.warning(
                f"Auto-approving RISKY step {step.step_number} (CI mode)",
                {"description": step.description},
            )
            return True

        print(f"\n⚠️  RISKY ACTION — Step {step.step_number}")
        print(f"   Description: {step.description}")
        print(f"   Locator:     {step.locator.primary if step.locator else 'N/A'}")
        answer = input("   Approve? (yes/no/escalate): ").strip().lower()

        if answer in ("y", "yes"):
            self.logger.info(f"Operator approved risky step {step.step_number}", {})
            return True
        if answer in ("e", "escalate"):
            raise ReplayError(
                f"Operator escalated at step {step.step_number}",
                step_number=step.step_number,
            )
        self.logger.warning(f"Operator rejected risky step {step.step_number}", {})
        return False

    # ------------------------------------------------------------------
    # Dialog handler
    # ------------------------------------------------------------------

    def _install_dialog_handler(self) -> None:
        """Auto-dismiss unexpected dialogs (Tier 2 recovery)."""
        async def _handler(dialog):
            self.logger.warning(f"Unexpected dialog dismissed: {dialog.message}", {})
            await dialog.dismiss()

        self.page.on("dialog", _handler)

    # ------------------------------------------------------------------
    # Authentication (engine-level session setup, not an artifact step —
    # see the BANK_USERNAME/BANK_PASSWORD comment near the top of this file)
    # ------------------------------------------------------------------

    async def _login(self) -> None:
        if not (BANK_USERNAME and BANK_PASSWORD):
            raise ReplayError(
                "No BANK_USERNAME/BANK_PASSWORD configured — cannot authenticate.",
                step_number=0,
            )
        await self.page.goto("http://127.0.0.1:5001/login", wait_until="domcontentloaded", timeout=15_000)
        await asyncio.sleep(1)
        if "login" in self.page.url:
            await self.page.locator("input[name='username']").fill(BANK_USERNAME)
            await self.page.locator("input[name='password']").fill(BANK_PASSWORD)
            await self.page.locator("button[type='submit']").click()
            await self.page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(1)
        self.logger.debug("Authenticated", {"url": self.page.url})

    # ------------------------------------------------------------------
    # Screenshot helper
    # ------------------------------------------------------------------

    async def _screenshot(self, name: str) -> Path:
        ts = int(time.time() * 1000)
        run_dir = EVIDENCE_DIR / self.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        path = run_dir / f"{ts}_{name}.png"
        await self.page.screenshot(path=str(path))
        return path

    # ------------------------------------------------------------------
    # Main run
    # ------------------------------------------------------------------

    async def run(
        self,
        artifact: Artifact,
        params: dict[str, str] | None = None,
    ) -> ReplayResult:
        """
        Execute every step in the artifact in order.
        Returns ReplayResult.
        """
        assert self.page is not None, "Use inside async with ReplayEngine()"
        params = params or {}
        self._install_dialog_handler()

        t0 = time.monotonic()
        accumulated_output: dict[str, Any] = {}
        steps_run = 0
        error_shot: str | None = None

        self.logger.info("Replay started", {
            "artifact_id": artifact.artifact_id,
            "task_id":     artifact.task.task_id,
            "params":      params,
        })


        # Authenticate before replaying. See the BANK_USERNAME/BANK_PASSWORD
        # comment above — this is engine-level session setup, not part of
        # the replayed artifact itself.
        await self._login()
        self.logger.debug("Starting replay", {"url": self.page.url})

        for step in artifact.steps:
        # Skip login steps — replay handles login in setup
            login_selectors = {"input[name='username']", "input[name='password']", "button[type='submit']"}
            is_login_step = (
                "login" in step.page_url or
                (step.locator and step.locator.primary in login_selectors and step.step_number <= 5)
            )
            if is_login_step:
                self.logger.info(f"Skipping login step {step.step_number}", {})
                continue
              

            # --- Guardrail check ---
            ok, reason = self.guardrails.check(
                url         = self.page.url,
                action_kind = step.action_kind,
                selector    = step.locator.primary if step.locator else None,
                value       = step.inputs[0].value if step.inputs else None,
                description = step.description,
            )
            if not ok:
                shot = await self._screenshot(f"guardrail_block_step{step.step_number}")
                raise ReplayError(
                    f"Guardrail blocked step {step.step_number}: {reason}",
                    step_number=step.step_number,
                    screenshot_path=str(shot),
                )

            # --- Confirmation gate for risky steps ---
            if step.risk_level == "risky":
                approved = await self._confirm_risky_step(step)
                if not approved:
                    total_ms = int((time.monotonic() - t0) * 1000)
                    return ReplayResult(
                        artifact_id   = artifact.artifact_id,
                        task_id       = artifact.task.task_id,
                        success       = False,
                        output        = accumulated_output,
                        steps_run     = steps_run,
                        total_time_ms = total_ms,
                        stop_reason   = "escalated",
                        error         = f"Operator rejected step {step.step_number}",
                        error_step    = step.step_number,
                    )

            # --- Execute with retry ---
            last_exc: Exception | None = None
            for attempt in range(step.retry_count + 1):
                try:
                    step_output = await _execute_step(self.page, step, params, self.logger)
                    accumulated_output.update(step_output)
                    last_exc = None
                    break

                except BusinessOutcome as bo:
                    # Tier 1 — not a crash, it's a valid answer
                    steps_run += 1
                    self.logger.info(f"Business outcome at step {step.step_number}: {bo.reason}", {
                        "outcome": bo.reason, "data": bo.data
                    })
                    total_ms = int((time.monotonic() - t0) * 1000)
                    return ReplayResult(
                        artifact_id      = artifact.artifact_id,
                        task_id          = artifact.task.task_id,
                        success          = True,   # valid outcome, not an error
                        output           = {**accumulated_output, "business_outcome": bo.reason},
                        steps_run        = steps_run,
                        total_time_ms    = total_ms,
                        stop_reason      = "business_outcome",
                        business_outcome = bo.reason,
                    )

                except SessionExpiredError as se:
                    # Tier 2 (specialized) — re-authenticate, then retry.
                    # A plain retry/backoff would just hit the login page
                    # again and again; the recovery action here is specific
                    # to *why* the step failed, not a generic wait-and-hope.
                    self.logger.warning(
                        f"Session expired at step {step.step_number}, re-authenticating "
                        f"(attempt {attempt + 1})",
                        {"attempt": attempt + 1},
                    )
                    last_exc = se
                    if attempt < step.retry_count:
                        await self._login()
                        await asyncio.sleep(1.0)
                    continue

                except RecoverableError as re_:
                    # Tier 2 — retry
                    self.logger.warning(
                        f"Recoverable error step {step.step_number} attempt {attempt + 1}: {re_}",
                        {"attempt": attempt + 1},
                    )
                    last_exc = re_
                    if attempt < step.retry_count:
                        await asyncio.sleep(1.5 * (attempt + 1))   # back-off
                    continue

                except ReplayError:
                    raise   # already a Tier 3 — bubble up

                except Exception as exc:
                    last_exc = RecoverableError(str(exc))
                    if attempt < step.retry_count:
                        await asyncio.sleep(1.5 * (attempt + 1))
                    continue

            if last_exc is not None:
                # Exhausted retries → Tier 3
                shot = await self._screenshot(f"hard_fail_step{step.step_number}")
                self.logger.error(
                    f"Hard failure at step {step.step_number}: {last_exc}",
                    {"screenshot": str(shot)},
                )
                raise ReplayError(
                    reason          = f"Step {step.step_number} failed after {step.retry_count + 1} attempts: {last_exc}",
                    step_number     = step.step_number,
                    screenshot_path = str(shot),
                )

            steps_run += 1

            # --- Checkpoint verification ---
            if not await _verify_checkpoints(self.page, step):
                self.logger.warning(
                    f"Checkpoint mismatch at step {step.step_number} — continuing",
                    {"url": self.page.url},
                )

        # All steps complete
        total_ms = int((time.monotonic() - t0) * 1000)
        self.logger.info("Replay complete", {
            "steps_run":    steps_run,
            "total_time_ms": total_ms,
            "output":       accumulated_output,
        })

        return ReplayResult(
            artifact_id   = artifact.artifact_id,
            task_id       = artifact.task.task_id,
            success       = True,
            output        = accumulated_output,
            steps_run     = steps_run,
            total_time_ms = total_ms,
            stop_reason   = "complete",
        )


# ---------------------------------------------------------------------------
# Convenience function
# ---------------------------------------------------------------------------

async def replay_artifact(
    artifact_path: Path,
    params: dict[str, str] | None = None,
    headless: bool = True,
    require_confirmation: bool = True,
    run_id: str | None = None,
) -> ReplayResult:
    artifact = Artifact.load(artifact_path)
    async with ReplayEngine(
        headless=headless,
        require_confirmation=require_confirmation,
        run_id=run_id,
    ) as engine:
        return await engine.run(artifact, params=params)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv
    load_dotenv()

    if len(sys.argv) < 2:
        print("Usage: python replay.py <artifact.json> [key=value ...]")
        sys.exit(1)

    artifact_path = Path(sys.argv[1])
    params = {}
    for arg in sys.argv[2:]:
        if "=" in arg:
            k, v = arg.split("=", 1)
            params[k] = v

    print(f"Replaying {artifact_path.name} with params: {params}")
    try:
        result = asyncio.run(
            replay_artifact(artifact_path, params=params, headless=False)
        )
        print(f"\nResult: {'SUCCESS' if result.success else 'FAILED'}")
        print(f"Stop reason: {result.stop_reason}")
        print(f"Steps run: {result.steps_run}")
        print(f"Output: {json.dumps(result.output, indent=2)}")
        if result.error:
            print(f"Error: {result.error} (step {result.error_step})")
    except ReplayError as exc:
        print(f"\n❌ Hard failure: {exc.reason}")
        print(f"   At step: {exc.step_number}")
        if exc.screenshot_path:
            print(f"   Screenshot: {exc.screenshot_path}")
        sys.exit(2)