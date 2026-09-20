"""
src/agent.py
------------
AI Agent Loop — Claude claude-sonnet-4-6 vision drives Playwright.

Flow:
  1. Take screenshot of current browser state
  2. Send screenshot + goal + history to Claude
  3. Claude returns a structured action (click / type / navigate / done / escalate)
  4. Execute action via Playwright
  5. Repeat until: goal met | max_steps | timeout | dead-end | escalation needed

Claude never sees raw DOM — only screenshots (pixel-level grounding).
All decisions are recorded for artifact construction in artifact.py.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import anthropic
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from dotenv import load_dotenv

# Must run before the env reads below. No-op if a caller (run_tasks.py)
# already loaded .env — required here too since `python src/agent.py` makes
# this module the entry point, and module-level code runs before any
# `if __name__` guard at the bottom of the file.
load_dotenv()

from guardrails import GuardrailsEngine, ActionType
from logger import AgentLogger
from escalation import request_escalation, HandoffResult

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
MODEL = "claude-sonnet-4-6"
BASE_URL = "http://127.0.0.1:5001"
MAX_STEPS = 30
STEP_TIMEOUT_S = 30          # seconds per step before we treat it as stuck
SESSION_TIMEOUT_S = 300      # 5-minute hard cap per run
EVIDENCE_DIR = Path(__file__).parent.parent / "evidence"
ARTIFACTS_DIR = Path(__file__).parent.parent / "artifacts"

# Login credentials are never hardcoded in source, prompts, artifacts, or
# logs — they are read from env at runtime only. guardrails.redact() also
# scrubs the password value from any string before it is persisted, as a
# second line of defense.
BANK_USERNAME = os.getenv("BANK_USERNAME", "")
BANK_PASSWORD = os.getenv("BANK_PASSWORD", "")

EVIDENCE_DIR.mkdir(exist_ok=True)
ARTIFACTS_DIR.mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

class ActionKind(str, Enum):
    NAVIGATE  = "navigate"
    CLICK     = "click"
    TYPE      = "type"
    SELECT    = "select"
    WAIT      = "wait"
    SCREENSHOT = "screenshot"   # explicit pause-and-observe
    DONE      = "done"
    ESCALATE  = "escalate"      # human handoff requested


@dataclass
class AgentAction:
    kind:        ActionKind
    selector:    str | None  = None   # CSS / text selector
    value:       str | None  = None   # URL for navigate; text for type
    description: str         = ""     # human-readable explanation
    reasoning:   str         = ""     # Claude's chain-of-thought

    # Populated after execution
    success:     bool        = False
    error:       str | None  = None
    duration_ms: int         = 0
    screenshot_path: str | None = None
    extracted_data: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentStep:
    step_number:  int
    timestamp_ms: int
    action:       AgentAction
    page_url:     str
    page_title:   str


@dataclass
class AgentResult:
    goal:           str
    success:        bool
    steps:          list[AgentStep]
    extracted_data: dict[str, Any]
    total_time_ms:  int
    stop_reason:    str   # "goal_met" | "max_steps" | "timeout" | "dead_end" | "escalated" | "escalation_timeout" | "guardrail_block" | "error"
    escalation_needed: bool = False
    error_message:  str | None = None


# ---------------------------------------------------------------------------
# Claude prompting
# ---------------------------------------------------------------------------

def _build_system_prompt() -> str:
    """
    Build the system prompt with credentials injected at call time from env
    (never hardcoded in source). If credentials are not configured, the
    login instructions are omitted and the agent is expected to escalate
    when it hits a login form it cannot complete.
    """
    if BANK_USERNAME and BANK_PASSWORD:
        login_block = f"""\
- If you see a login form, you MUST complete these steps IN ORDER:
  1. First action: click the username field, selector: "input[name='username']"
  2. Second action: type "{BANK_USERNAME}" into it
  3. Third action: click the password field, selector: "input[name='password']"
  4. Fourth action: type "{BANK_PASSWORD}" into it
  5. Fifth action: click the login button, selector: "button[type='submit']"
  Do NOT skip steps. Do NOT combine steps.\
"""
    else:
        login_block = (
            "- If you see a login form and no credentials are configured, "
            "return action=escalate with escalate_reason explaining that "
            "credentials are required."
        )

    return f"""\
You are an AI banking agent that controls a web browser by reading screenshots.
Your job is to accomplish a user goal by issuing ONE action at a time.

RULES:
- Analyze the screenshot carefully before acting.
- Choose the most direct path to the goal.
{login_block}
- If the goal is already accomplished, return action=done with extracted_data.
- If you are stuck (same page 3+ steps, no progress), return action=escalate.
- Never invent data — only extract what is literally visible on screen.
- Do not expose credentials or PII in your reasoning field.

SELECTOR GUIDANCE:
- Prefer: CSS selectors like "a[href='/search']" or "button[type='submit']"
- For text selectors use exact case from the page e.g. "text=Search Member" not "text=SEARCH MEMBER"
- Then: placeholder text (e.g., "placeholder=Member ID")
- Then: CSS class / id only if unambiguous
- For forms: use label text to identify inputs

OUTPUT FORMAT — return ONLY valid JSON, no markdown fences:
{{
  "action": "navigate|click|type|select|wait|done|escalate",
  "selector": "<css or text selector, null if not needed>",
  "value": "<URL for navigate, text for type, null otherwise>",
  "description": "<one sentence of what you are doing>",
  "reasoning": "<why this is the right next step>",
  "extracted_data": {{<only on done — key/value pairs of goal output>}},
  "escalate_reason": "<only on escalate — why human is needed>"
}}
"""


SYSTEM_PROMPT = _build_system_prompt()


def _encode_screenshot(path: Path) -> str:
    """Return base64-encoded PNG for the Anthropic messages API."""
    return base64.standard_b64encode(path.read_bytes()).decode("utf-8")


def _build_messages(
    goal: str,
    history: list[AgentStep],
    screenshot_path: Path,
) -> list[dict]:
    """Construct the messages list for the Claude API call."""

    history_text = ""
    if history:
        lines = []
        for s in history[-6:]:   # keep last 6 steps to stay within context
            a = s.action
            lines.append(
                f"Step {s.step_number}: [{a.kind}] {a.description} "
                f"— {'OK' if a.success else 'FAILED'}"
            )
        history_text = "\n".join(lines)

    user_content: list[dict] = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": _encode_screenshot(screenshot_path),
            },
        },
        {
            "type": "text",
            "text": (
                f"GOAL: {goal}\n\n"
                f"RECENT HISTORY:\n{history_text or 'None yet — first step.'}\n\n"
                "What is the single best next action? Return JSON only."
            ),
        },
    ]

    return [{"role": "user", "content": user_content}]


# ---------------------------------------------------------------------------
# Browser helpers
# ---------------------------------------------------------------------------

async def _screenshot(page: Page, name: str) -> Path:
    """Take a screenshot and save it to evidence/. Returns the path."""
    ts = int(time.time() * 1000)
    path = EVIDENCE_DIR / f"{ts}_{name}.png"
    # In headed mode, an unfocused window can starve Chromium's paint loop
    # and hang the screenshot's font-load wait indefinitely — bring it to
    # front first so rendering isn't throttled.
    await page.bring_to_front()
    await page.screenshot(path=str(path), full_page=False, scale="css", timeout=60000)
    return path
 


async def _execute_action(page: Page, action: AgentAction, logger: AgentLogger) -> None:
    """
    Execute a single Playwright action.
    Sets action.success / action.error in-place.
    """
    t0 = time.monotonic()
    try:
        if action.kind == ActionKind.NAVIGATE:
            value = action.value or ""
            if value.startswith("javascript:"):
                await page.evaluate(value[len("javascript:"):])
            else:
                await page.goto(value, wait_until="domcontentloaded", timeout=15_000)

        elif action.kind == ActionKind.CLICK:
            # Claude sometimes returns a comma-separated list of fallback
            # selectors (mixing CSS and text=/placeholder= forms), which
            # Playwright's CSS engine can't parse as one compound selector.
            # Try each candidate in order until one works.
            candidates = [c.strip() for c in (action.selector or "").split(",") if c.strip()]
            last_exc: Exception | None = None
            for sel in candidates or [""]:
                try:
                    if sel.startswith("text="):
                        await page.get_by_text(sel[5:], exact=False).first.click(timeout=10_000)
                    elif sel.startswith("placeholder="):
                        await page.get_by_placeholder(sel[12:]).first.click(timeout=10_000)
                    else:
                        await page.locator(sel).first.click(timeout=10_000)
                    last_exc = None
                    break
                except Exception as exc:
                    last_exc = exc
                    continue
            if last_exc is not None:
                raise last_exc

        elif action.kind == ActionKind.TYPE:
            sel = action.selector or ""
            if sel.startswith("placeholder="):
                loc = page.get_by_placeholder(sel[12:]).first
            elif sel.startswith("text="):
                loc = page.get_by_label(sel[5:]).first
            else:
                loc = page.locator(sel).first
            await loc.clear()
            await loc.type(action.value or "", delay=40)

        elif action.kind == ActionKind.SELECT:
            sel = action.selector or "select"
            val = action.value or ""
            try:
                await page.locator(sel).select_option(value=val, timeout=10_000)
            except Exception:
                await page.locator(sel).select_option(label=val, timeout=10_000)

        elif action.kind == ActionKind.WAIT:
            try:
                secs = float(action.value or "1")
            except ValueError:
                secs = 1.0
            await asyncio.sleep(min(secs, 5))

        elif action.kind in (ActionKind.DONE, ActionKind.ESCALATE, ActionKind.SCREENSHOT):
            pass   # handled by the loop

        # Small stabilization pause after every action
        await asyncio.sleep(0.6)
        action.success = True

    except Exception as exc:
        action.success = False
        action.error = str(exc)
        logger.warning(f"Action failed: {action.kind} — {exc}")

    action.duration_ms = int((time.monotonic() - t0) * 1000)


# ---------------------------------------------------------------------------
# Dead-end detection
# ---------------------------------------------------------------------------

def _is_dead_end(history: list[AgentStep], window: int = 6) -> bool:
    """
    Return True if the last `window` steps all failed or all hit the same URL
    with no progress — signals a stuck agent.
    """
    if len(history) < window:
        return False
    recent = history[-window:]
    # All failed
    if all(not s.action.success for s in recent):
        return True
    # All same URL with no extraction AND all actions are the same kind
    urls = {s.page_url for s in recent}
    kinds = {s.action.kind for s in recent}
    if len(urls) == 1 and len(kinds) == 1 and not any(s.action.extracted_data for s in recent):
        return True
    return False


# ---------------------------------------------------------------------------
# Core agent loop
# ---------------------------------------------------------------------------

class BankingAgent:
    """
    Vision-based agent that drives Playwright using Claude claude-sonnet-4-6.

    Usage:
        async with BankingAgent() as agent:
            result = await agent.run("Look up member 482915 balance")
    """

    def __init__(
        self,
        headless: bool = True,
        run_id: str | None = None,
        enable_escalation: bool = True,
        escalation_timeout_s: int = 120,
    ):
        self.headless  = headless
        self.run_id    = run_id or f"run_{int(time.time())}"
        # Whether a dead-end / explicit escalate should actually pause and
        # wait for a human (real handoff) or just stop immediately and
        # report escalation_needed (used by unattended CI/demo runs where no
        # human is present to click "Resume").
        self.enable_escalation    = enable_escalation
        self.escalation_timeout_s = escalation_timeout_s
        _workspace_id  = os.getenv("ANTHROPIC_WORKSPACE_ID", "")
        self.client    = anthropic.Anthropic(
            api_key=ANTHROPIC_API_KEY,
            default_headers={"anthropic-workspace-id": _workspace_id} if _workspace_id else {},
        )
        self.guardrails = GuardrailsEngine()
        self.logger    = AgentLogger(run_id=self.run_id, evidence_dir=EVIDENCE_DIR)
        self._playwright: Playwright | None = None
        self._browser:    Browser | None    = None
        self._context:    BrowserContext | None = None
        self.page:        Page | None       = None

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "BankingAgent":
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=["--no-sandbox"],
        )
        self._context = await self._browser.new_context(
            viewport={"width": 900, "height": 700},
            locale="en-US",
        )
        self.page = await self._context.new_page()
        self.logger.info("Browser launched", {"run_id": self.run_id})
        return self

    async def __aexit__(self, *_) -> None:
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        self.logger.info("Browser closed", {"run_id": self.run_id})

    # ------------------------------------------------------------------
    # Claude decision
    # ------------------------------------------------------------------

    def _ask_claude(self, goal: str, history: list[AgentStep], screenshot_path: Path) -> dict:
        """Call Claude with the screenshot and return the parsed action dict."""
        messages = _build_messages(goal, history, screenshot_path)

        last_exc: Exception | None = None
        for attempt in range(3):
            response = self.client.messages.create(
                model=MODEL,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                messages=messages,
            )

            text_blocks = [b.text for b in response.content if getattr(b, "type", None) == "text"]
            raw = "".join(text_blocks).strip()

            # Strip markdown fences if Claude adds them despite instructions
            if raw.startswith("```"):
                parts = raw.split("```")
                raw = parts[1] if len(parts) > 1 else ""
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()

            # Claude sometimes prefaces the JSON with a sentence of prose
            # despite instructions to return JSON only — skip to the first
            # object boundary before parsing.
            if raw and not raw.startswith("{"):
                start = raw.find("{")
                if start != -1:
                    raw = raw[start:]

            if not raw:
                last_exc = ValueError(
                    f"Empty response from Claude (stop_reason={response.stop_reason})"
                )
                self.logger.warning(
                    "Empty/unparseable Claude response, retrying",
                    {"attempt": attempt + 1, "stop_reason": response.stop_reason},
                )
                continue

            try:
                # Deliberately NOT json.loads(raw): that requires the ENTIRE
                # string to be exactly one JSON value, so it throws
                # "Extra data" the moment Claude appends anything after a
                # perfectly valid object — trailing commentary, a stray
                # newline plus more text, etc. This happened repeatedly in
                # practice (caught via real discovery runs, not a
                # hypothetical). raw_decode() parses one JSON value starting
                # at position 0 and simply ignores whatever comes after it,
                # which is exactly the tolerance needed here: we only ever
                # want the first action object, never a multi-document
                # response.
                obj, _ = json.JSONDecoder().raw_decode(raw)
                return obj
            except json.JSONDecodeError as exc:
                last_exc = exc
                self.logger.warning(
                    "Failed to parse Claude response as JSON, retrying",
                    {"attempt": attempt + 1, "raw": raw[:500]},
                )
                continue

        raise last_exc or ValueError("Failed to get a valid response from Claude")

    # ------------------------------------------------------------------
    # Human handoff
    # ------------------------------------------------------------------

    async def _handoff(
        self,
        goal: str,
        step_num: int,
        reason: str,
        trigger: str,
    ) -> HandoffResult | None:
        """
        Pause for a human operator on the live session. Returns None if
        escalation is disabled (unattended mode) — caller should treat that
        the same as "not resumed".
        """
        if not self.enable_escalation:
            self.logger.warning(
                "Escalation disabled (unattended mode) — not pausing for a human",
                {"trigger": trigger, "reason": reason},
            )
            return None

        return await request_escalation(
            page      = self.page,
            run_id    = self.run_id,
            goal      = goal,
            step_num  = step_num,
            reason    = reason,
            trigger   = trigger,
            logger    = self.logger,
            timeout_s = self.escalation_timeout_s,
        )

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self, goal: str, force_escalate_at_step: int | None = None) -> AgentResult:
        """
        Drive the browser toward `goal` using Claude vision in a loop.
        Returns AgentResult with all steps and extracted data.

        `force_escalate_at_step` is a demo/test hook only: when set, the
        loop triggers the exact same handoff path used by a real dead-end
        at that step number, so the escalation mechanism can be exercised
        deterministically (e.g. for evidence capture) instead of waiting
        for the agent to organically get stuck. It does not add a separate
        code path — it calls the same `_handoff()` used by dead-end/escalate.
        """
        assert self.page is not None, "Call inside async with BankingAgent()"

        history:        list[AgentStep]     = []
        extracted_data: dict[str, Any]      = {}
        session_start   = time.monotonic()
        stop_reason     = "max_steps"
        escalation_needed = False
        error_message: str | None = None

        self.logger.info("Agent run started", {"goal": goal, "run_id": self.run_id})

        # Always start at the portal root
        await self.page.goto(BASE_URL, wait_until="domcontentloaded")
        await asyncio.sleep(0.5)

        for step_num in range(1, MAX_STEPS + 1):

            # --- Hard session timeout ---
            if time.monotonic() - session_start > SESSION_TIMEOUT_S:
                stop_reason = "timeout"
                self.logger.warning("Session timeout reached", {"step": step_num})
                break

            # --- Screenshot ---
            shot_path = await _screenshot(self.page, f"step_{step_num:02d}")

            # --- Forced escalation (demo/test hook, see docstring) ---
            if force_escalate_at_step == step_num:
                self.logger.warning("Forced escalation demo hook triggered", {"step": step_num})
                escalation_needed = True
                handoff = await self._handoff(
                    goal     = goal,
                    step_num = step_num,
                    reason   = "Demo: escalation forced for evidence capture.",
                    trigger  = "manual",
                )
                if handoff is not None and handoff.resumed:
                    handoff_action = AgentAction(
                        kind        = ActionKind.ESCALATE,
                        description = "Human operator took control and resumed automation",
                        reasoning   = handoff.human_notes or "(no notes provided)",
                        success     = True,
                    )
                    history.append(AgentStep(
                        step_number  = step_num,
                        timestamp_ms = int(time.time() * 1000),
                        action       = handoff_action,
                        page_url     = self.page.url,
                        page_title   = await self.page.title(),
                    ))
                    continue
                stop_reason = "escalation_timeout" if handoff is not None else "escalated"
                break

            # --- Dead-end check ---
            if _is_dead_end(history):
                self.logger.warning("Dead-end detected", {"step": step_num})
                # Take failure screenshot
                fail_shot = await _screenshot(self.page, f"dead_end_{step_num:02d}")
                self.logger.screenshot(fail_shot, "dead_end")
                escalation_needed = True

                handoff = await self._handoff(
                    goal     = goal,
                    step_num = step_num,
                    reason   = f"Agent made no progress for {len(history)} consecutive steps.",
                    trigger  = "dead_end",
                )

                if handoff is not None and handoff.resumed:
                    # A human took over on the live session and resumed it.
                    # Record what they did as a synthetic step so it shows up
                    # in the run history/evidence, then let the agent
                    # re-observe the (now-changed) page and keep going.
                    handoff_action = AgentAction(
                        kind        = ActionKind.ESCALATE,
                        description = "Human operator took control and resumed automation",
                        reasoning   = handoff.human_notes or "(no notes provided)",
                        success     = True,
                    )
                    history.append(AgentStep(
                        step_number  = step_num,
                        timestamp_ms = int(time.time() * 1000),
                        action       = handoff_action,
                        page_url     = self.page.url,
                        page_title   = await self.page.title(),
                    ))
                    self.logger.info("Resuming automation after human handoff", {
                        "human_actions": len(handoff.human_actions),
                        "notes":         handoff.human_notes,
                    })
                    continue

                stop_reason = "dead_end"
                if handoff is not None and not handoff.resumed:
                    stop_reason = "escalation_timeout"
                    error_message = "Human handoff timed out with no resume signal."
                break

            # --- Ask Claude ---
            try:
                raw_action = self._ask_claude(goal, history, shot_path)
            except Exception as exc:
                error_message = f"Claude API error at step {step_num}: {exc}"
                self.logger.error(error_message, {})
                stop_reason = "error"
                break

            # --- Parse Claude response ---
            try:
                kind = ActionKind(raw_action["action"])
            except ValueError:
                kind = ActionKind.WAIT   # safe fallback

            action = AgentAction(
                kind        = kind,
                selector    = raw_action.get("selector"),
                value       = raw_action.get("value"),
                description = raw_action.get("description", ""),
                reasoning   = raw_action.get("reasoning", ""),
                extracted_data = raw_action.get("extracted_data", {}),
            )
            action.screenshot_path = str(shot_path)

            # --- Guardrails check ---
            permitted, reason = self.guardrails.check(
                url         = self.page.url,
                action_kind = action.kind.value,
                selector    = action.selector,
                value       = action.value,
                description = action.description,
            )
            if not permitted:
                action.success = False
                action.error   = f"Guardrail blocked: {reason}"
                self.logger.warning("Guardrail blocked action", {
                    "step": step_num,
                    "reason": reason,
                    "action": action.kind.value,
                })
                # Record the step but stop
                step = AgentStep(
                    step_number  = step_num,
                    timestamp_ms = int(time.time() * 1000),
                    action       = action,
                    page_url     = self.page.url,
                    page_title   = await self.page.title(),
                )
                history.append(step)
                self.logger.step(step)
                stop_reason = "guardrail_block"
                break

            # --- Terminal conditions before execution ---
            if kind == ActionKind.DONE:
                extracted_data = raw_action.get("extracted_data", {})
                stop_reason = "goal_met"
                # Record final step
                action.success = True
                step = AgentStep(
                    step_number  = step_num,
                    timestamp_ms = int(time.time() * 1000),
                    action       = action,
                    page_url     = self.page.url,
                    page_title   = await self.page.title(),
                )
                history.append(step)
                self.logger.step(step)
                self.logger.info("Goal met", {"extracted": extracted_data})
                break

            if kind == ActionKind.ESCALATE:
                escalation_needed = True
                action.success = True
                action.error   = raw_action.get("escalate_reason", "Agent requested escalation")
                step = AgentStep(
                    step_number  = step_num,
                    timestamp_ms = int(time.time() * 1000),
                    action       = action,
                    page_url     = self.page.url,
                    page_title   = await self.page.title(),
                )
                history.append(step)
                self.logger.step(step)
                self.logger.warning("Escalation triggered", {"reason": action.error})

                handoff = await self._handoff(
                    goal     = goal,
                    step_num = step_num,
                    reason   = action.error,
                    trigger  = "agent_requested",
                )

                if handoff is not None and handoff.resumed:
                    self.logger.info("Resuming automation after human handoff", {
                        "human_actions": len(handoff.human_actions),
                        "notes":         handoff.human_notes,
                    })
                    continue

                stop_reason = "escalated"
                if handoff is not None and not handoff.resumed:
                    stop_reason = "escalation_timeout"
                break

            # --- Execute action ---
            await _execute_action(self.page, action, self.logger)

            # Screenshot after action (for evidence)
            post_shot = await _screenshot(self.page, f"step_{step_num:02d}_post")
            action.screenshot_path = str(post_shot)   # overwrite with post-action shot

            # --- Record step ---
            step = AgentStep(
                step_number  = step_num,
                timestamp_ms = int(time.time() * 1000),
                action       = action,
                page_url     = self.page.url,
                page_title   = await self.page.title(),
            )
            history.append(step)
            self.logger.step(step)

            # Log failure screenshot
            if not action.success:
                self.logger.screenshot(Path(action.screenshot_path), "action_failure")

        # --- Build result ---
        total_ms = int((time.monotonic() - session_start) * 1000)
        result = AgentResult(
            goal              = goal,
            success           = stop_reason == "goal_met",
            steps             = history,
            extracted_data    = extracted_data,
            total_time_ms     = total_ms,
            stop_reason       = stop_reason,
            escalation_needed = escalation_needed,
            error_message     = error_message,
        )

        self.logger.info("Agent run complete", {
            "stop_reason":  stop_reason,
            "total_steps":  len(history),
            "total_time_ms": total_ms,
            "success":      result.success,
        })

        return result


# ---------------------------------------------------------------------------
# Convenience runner (used by orchestrator / tests)
# ---------------------------------------------------------------------------

async def run_agent(
    goal: str,
    headless: bool = True,
    run_id: str | None = None,
    enable_escalation: bool = True,
) -> AgentResult:
    """Run the agent for a single goal and return the result."""
    async with BankingAgent(
        headless=headless, run_id=run_id, enable_escalation=enable_escalation,
    ) as agent:
        return await agent.run(goal)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

async def _run_cli(goal: str, escalate_demo_step: int | None) -> AgentResult:
    async with BankingAgent(headless=False, enable_escalation=True) as agent:
        return await agent.run(goal, force_escalate_at_step=escalate_demo_step)


if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv
    load_dotenv()

    args = sys.argv[1:]
    escalate_demo_step: int | None = None
    if "--escalate-demo" in args:
        # Real end-to-end handoff demo: pauses at step 2, opens the
        # oversight page at http://127.0.0.1:7777, waits for a human to
        # click "Resume Automation", then continues the same run.
        args = [a for a in args if a != "--escalate-demo"]
        escalate_demo_step = 2

    goal = " ".join(args) or "Look up member 482915 and read their current savings balance"
    print(f"Running agent with goal: {goal!r}")
    result = asyncio.run(_run_cli(goal, escalate_demo_step))

    print(f"\nResult: {'SUCCESS' if result.success else 'FAILED'}")
    print(f"Stop reason: {result.stop_reason}")
    print(f"Steps taken: {len(result.steps)}")
    print(f"Extracted data: {json.dumps(result.extracted_data, indent=2)}")
    if result.error_message:
        print(f"Error: {result.error_message}")
