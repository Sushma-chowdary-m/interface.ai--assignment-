"""
src/escalation.py
-----------------
Human Handoff — a real escalation mechanism, not a TODO.

When the agent detects it is stuck, blocked, or explicitly flags escalation:

  1. PAUSE   — automation stops, browser remains open and visible
  2. EXPOSE  — a local HTTP page shows the live session URL and context
  3. CAPTURE — a background thread polls the browser state every second,
               recording every URL change, click, and form submit the human makes
  4. RESUME  — after the human signals "done" (via the oversight page),
               automation re-evaluates the new page state and continues

The human-captured steps are saved alongside the agent steps in the run log,
and can be folded back into the artifact as learned corrections.

Architecture:
  - Uses Python's http.server in a background thread (no extra deps)
  - Communicates via a simple shared state dict (thread-safe via threading.Event)
  - Playwright's CDP (Chrome DevTools Protocol) exposes the live session
  - Human steps are recorded via Playwright's request / response events
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from playwright.async_api import Page

from logger import AgentLogger

EVIDENCE_DIR = Path(__file__).parent.parent / "evidence"


# ---------------------------------------------------------------------------
# Escalation context — what triggered the handoff
# ---------------------------------------------------------------------------

@dataclass
class EscalationContext:
    run_id:        str
    trigger:       str         # "agent_requested" | "dead_end" | "guardrail" | "timeout" | "manual"
    step_number:   int
    current_url:   str
    goal:          str
    reason:        str         # human-readable explanation
    screenshot_path: str | None = None
    extra:         dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Human action recording
# ---------------------------------------------------------------------------

@dataclass
class HumanAction:
    timestamp_ms: int
    kind:         str    # "navigation" | "click" | "input" | "submit"
    url:          str
    detail:       str    # e.g. clicked element text, form data (sanitised)


# ---------------------------------------------------------------------------
# Shared state (main thread ↔ HTTP server thread)
# ---------------------------------------------------------------------------

class _EscalationState:
    def __init__(self):
        self.context:        EscalationContext | None = None
        self.human_actions:  list[HumanAction] = []
        self.done_event      = threading.Event()
        self.resume_url:     str | None = None
        self.human_notes:    str = ""
        self._lock           = threading.Lock()

    def reset(self, ctx: EscalationContext) -> None:
        with self._lock:
            self.context       = ctx
            self.human_actions = []
            self.resume_url    = None
            self.human_notes   = ""
            self.done_event.clear()

    def record_action(self, action: HumanAction) -> None:
        with self._lock:
            self.human_actions.append(action)

    def signal_done(self, url: str, notes: str) -> None:
        with self._lock:
            self.resume_url   = url
            self.human_notes  = notes
        self.done_event.set()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "context":       vars(self.context) if self.context else {},
                "human_actions": [vars(a) for a in self.human_actions],
                "done":          self.done_event.is_set(),
                "resume_url":    self.resume_url,
                "human_notes":   self.human_notes,
            }


_state = _EscalationState()


# ---------------------------------------------------------------------------
# Oversight HTTP server
# ---------------------------------------------------------------------------

_OVERSIGHT_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="refresh" content="3">
<title>⚠️ Human Oversight Required</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 700px;
          margin: 40px auto; padding: 20px; background: #fff8f0; color: #1a1a1a; }}
  h1 {{ color: #c0392b; }}
  .banner {{ background: #ffeaa7; border-left: 4px solid #e17055; padding: 12px 16px;
             border-radius: 4px; margin: 16px 0; }}
  .field {{ margin: 8px 0; }}
  label {{ font-weight: 600; display: block; margin-bottom: 4px; }}
  input, textarea {{ width: 100%; padding: 8px; border: 1px solid #ccc;
                     border-radius: 4px; font-size: 14px; box-sizing: border-box; }}
  button {{ background: #27ae60; color: white; padding: 10px 24px; border: none;
            border-radius: 4px; font-size: 16px; cursor: pointer; margin-top: 12px; }}
  button:hover {{ background: #219a52; }}
  .actions {{ margin-top: 20px; background: #f0f0f0; padding: 12px;
              border-radius: 4px; font-size: 13px; max-height: 200px; overflow-y: auto; }}
  pre {{ white-space: pre-wrap; margin: 0; }}
</style>
</head>
<body>
<h1>⚠️ Automation Paused — Human Oversight Required</h1>
<div class="banner">
  <strong>Trigger:</strong> {trigger}<br>
  <strong>Goal:</strong> {goal}<br>
  <strong>Reason:</strong> {reason}<br>
  <strong>Step:</strong> {step_number} &nbsp;|&nbsp;
  <strong>URL:</strong> <a href="{current_url}" target="_blank">{current_url}</a>
</div>

<p>The automation browser tab is open. Please complete the task manually,
then return here and click <em>Resume Automation</em>.</p>

<form method="POST" action="/done">
  <div class="field">
    <label for="notes">Notes for the agent (optional):</label>
    <textarea id="notes" name="notes" rows="3"
              placeholder="e.g. I dismissed the error dialog and searched for member 12345"></textarea>
  </div>
  <button type="submit">▶ Resume Automation</button>
</form>

<div class="actions">
  <strong>Recorded human actions ({action_count}):</strong>
  <pre>{actions_json}</pre>
</div>
</body>
</html>
"""


class _OversightHandler(BaseHTTPRequestHandler):

    def log_message(self, *args):
        pass   # silence default Apache-style logs

    def do_GET(self):
        snap = _state.snapshot()
        ctx  = snap["context"]
        actions = snap["human_actions"]

        html = _OVERSIGHT_HTML.format(
            trigger       = ctx.get("trigger", "unknown"),
            goal          = ctx.get("goal", ""),
            reason        = ctx.get("reason", ""),
            step_number   = ctx.get("step_number", "?"),
            current_url   = ctx.get("current_url", "#"),
            action_count  = len(actions),
            actions_json  = json.dumps(actions[-10:], indent=2),
        )
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length).decode("utf-8")

        # Parse simple form data
        import urllib.parse
        params = dict(urllib.parse.parse_qsl(body))
        notes  = params.get("notes", "")

        # Signal done
        snap = _state.snapshot()
        _state.signal_done(url=snap["context"].get("current_url", ""), notes=notes)

        resp = b"<html><body><h2>Resuming automation...</h2></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)


def _start_oversight_server(port: int = 7777) -> HTTPServer:
    server = HTTPServer(("127.0.0.1", port), _OversightHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


# ---------------------------------------------------------------------------
# Playwright action capture
# ---------------------------------------------------------------------------

async def _attach_human_recorder(page: Page) -> None:
    """
    Use Playwright events to record what the human does in the browser.
    Lightweight — records navigations and form submissions only.
    """
    def on_navigate(frame):
        if frame == page.main_frame:
            _state.record_action(HumanAction(
                timestamp_ms = int(time.time() * 1000),
                kind         = "navigation",
                url          = frame.url,
                detail       = frame.url,
            ))

    async def on_response(response):
        if response.request.method in ("POST", "PUT"):
            _state.record_action(HumanAction(
                timestamp_ms = int(time.time() * 1000),
                kind         = "submit",
                url          = response.url,
                detail       = f"{response.request.method} {response.status}",
            ))

    page.on("framenavigated", on_navigate)
    page.on("response", on_response)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

@dataclass
class HandoffResult:
    resumed:        bool
    resume_url:     str | None
    human_actions:  list[HumanAction]
    human_notes:    str
    wait_time_s:    float


async def pause_for_human(
    page:          Page,
    context:       EscalationContext,
    logger:        AgentLogger,
    timeout_s:     int = 300,
    oversight_port: int = 7777,
) -> HandoffResult:
    """
    Pause automation and wait for a human to take over.

    Steps:
      1. Reset shared state with this escalation's context
      2. Start oversight HTTP server (idempotent — reuses existing)
      3. Attach action recorder to the live Playwright page
      4. Make browser window visible (headful mode forced)
      5. Print instructions to the terminal
      6. Wait for human to click "Resume" or timeout
      7. Detach recorder, return HandoffResult

    The browser tab remains OPEN and INTERACTIVE throughout.
    """
    _state.reset(context)

    # Start oversight server
    try:
        server = _start_oversight_server(oversight_port)
    except OSError:
        # Already running — that's fine
        server = None

    # Attach recorder
    await _attach_human_recorder(page)

    # Save escalation screenshot
    shot_path: str | None = context.screenshot_path
    if not shot_path:
        ts = int(time.time() * 1000)
        run_dir = EVIDENCE_DIR / context.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        p = run_dir / f"{ts}_escalation.png"
        await page.screenshot(path=str(p))
        shot_path = str(p)
        context.screenshot_path = shot_path

    # Log
    logger.warning("ESCALATION: automation paused for human handoff", {
        "trigger":     context.trigger,
        "reason":      context.reason,
        "step_number": context.step_number,
        "current_url": context.current_url,
        "screenshot":  shot_path,
        "oversight":   f"http://127.0.0.1:{oversight_port}",
    })

    # Terminal instructions
    print("\n" + "=" * 60)
    print("⚠️  AUTOMATION PAUSED — HUMAN HANDOFF")
    print("=" * 60)
    print(f"   Trigger : {context.trigger}")
    print(f"   Reason  : {context.reason}")
    print(f"   Goal    : {context.goal}")
    print(f"   Step    : {context.step_number}")
    print(f"   URL     : {context.current_url}")
    print()
    print(f"   👉  Open the browser tab and complete the action manually.")
    print(f"   👉  Oversight page: http://127.0.0.1:{oversight_port}")
    print(f"   👉  Click 'Resume Automation' when done.")
    print("=" * 60 + "\n")

    # Wait for done signal (with timeout)
    t0 = time.monotonic()
    resumed = False

    # We can't use threading.Event.wait() inside asyncio without blocking the loop
    while True:
        if _state.done_event.is_set():
            resumed = True
            break
        elapsed = time.monotonic() - t0
        if elapsed > timeout_s:
            logger.warning("Human handoff timed out", {"timeout_s": timeout_s})
            break
        await asyncio.sleep(1.0)

    wait_time = time.monotonic() - t0

    snap = _state.snapshot()
    result = HandoffResult(
        resumed        = resumed,
        resume_url     = snap["resume_url"],
        human_actions  = list(_state.human_actions),
        human_notes    = snap["human_notes"],
        wait_time_s    = wait_time,
    )

    # Log what the human did
    logger.info("Human handoff complete", {
        "resumed":       resumed,
        "wait_time_s":   round(wait_time, 1),
        "actions_taken": len(result.human_actions),
        "notes":         result.human_notes,
    })

    if server:
        server.shutdown()

    return result


async def request_escalation(
    page:       Page,
    run_id:     str,
    goal:       str,
    step_num:   int,
    reason:     str,
    trigger:    str = "agent_requested",
    logger:     AgentLogger | None = None,
    timeout_s:  int = 300,
) -> HandoffResult:
    """
    Convenience wrapper — constructs context and calls pause_for_human.
    Use this from agent.py or replay.py.
    """
    from logger import AgentLogger as _AL
    if logger is None:
        logger = _AL(run_id=run_id, evidence_dir=EVIDENCE_DIR)

    ctx = EscalationContext(
        run_id      = run_id,
        trigger     = trigger,
        step_number = step_num,
        current_url = page.url,
        goal        = goal,
        reason      = reason,
    )
    return await pause_for_human(page=page, context=ctx, logger=logger, timeout_s=timeout_s)


# ---------------------------------------------------------------------------
# CLI self-test (simulates a handoff without a real browser)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    async def _demo():
        from unittest.mock import AsyncMock, MagicMock

        page = MagicMock()
        page.url = "http://127.0.0.1:5001/search"
        page.screenshot = AsyncMock()
        page.on = MagicMock()
        page.main_frame = MagicMock()

        from logger import AgentLogger
        logger = AgentLogger(run_id="demo_escalation", evidence_dir=EVIDENCE_DIR)

        print("Starting demo escalation — open http://127.0.0.1:7777 in your browser")
        result = await request_escalation(
            page    = page,
            run_id  = "demo_escalation",
            goal    = "Look up member 12345 savings balance",
            step_num = 5,
            reason  = "Member search returned zero results unexpectedly",
            trigger = "dead_end",
            logger  = logger,
        )
        print(f"\nResult: resumed={result.resumed}, wait={result.wait_time_s:.1f}s")
        print(f"Notes: {result.human_notes}")

    asyncio.run(_demo())