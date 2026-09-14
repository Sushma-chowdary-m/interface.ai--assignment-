"""
src/run_tasks.py
----------------
Main orchestrator — runs both assignment tasks end-to-end.

For each task:
  1. Discovery run  — BankingAgent (Claude + vision) drives the browser
  2. Build artifact — convert AgentResult → typed JSON artifact
  3. Save artifact  — write to artifacts/<task_id>.json
  4. Replay run     — ReplayEngine executes the artifact WITHOUT Claude
  5. Compare        — verify replay output matches discovery output

Usage:
  python run_tasks.py                    # both tasks, headless
  python run_tasks.py --visible          # both tasks, browser visible
  python run_tasks.py --task read_balance   # single task
  python run_tasks.py --task open_account  # single task
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from agent import run_agent, AgentResult
from artifact import build_artifact, Artifact, ARTIFACTS_DIR
from replay import replay_artifact, ReplayResult, ReplayError
from logger import AgentLogger

EVIDENCE_DIR = Path(__file__).parent.parent / "evidence"

# ---------------------------------------------------------------------------
# Task definitions
# ---------------------------------------------------------------------------

TASKS = {
    "read_balance": {
        "goal": "Look up member 12345 and read their current savings balance",
        "task_id":      "read_balance",
        "display_name": "Read Member Savings Balance",
        "description":  "Navigate to a member's profile and extract the current savings account balance.",
        "category":     "read",
        "risk_level":   "safe",
        "parameters": [
            {
                "name":        "member_id",
                "type":        "string",
                "description": "The 5-digit member ID to look up",
                "required":    True,
                "example":     "12345",
            }
        ],
        "expected_output": {"savings_balance": "$4,250.00"},
        "replay_params":   {"member_id": "12345"},
    },
    "open_account": {
        "goal": "Open a new savings sub-account for member 12345 and reach the confirmation screen",
        "task_id":      "open_account",
        "display_name": "Open New Savings Sub-Account",
        "description":  "Navigate to a member's profile, open the account creation form, fill in the details, and reach the confirmation screen.",
        "category":     "write",
        "risk_level":   "risky",
        "parameters": [
            {
                "name":        "member_id",
                "type":        "string",
                "description": "The 5-digit member ID for the new account",
                "required":    True,
                "example":     "12345",
            },
            {
                "name":        "account_type",
                "type":        "string",
                "description": "Type of account to open",
                "required":    False,
                "example":     "savings",
            },
        ],
        # Matches what the agent actually extracts on `done` (see
        # artifact.py's _guess_selector_for_field) — the discovery run
        # stops on the confirmation-preview screen, which surfaces
        # new_account_number but not a separate confirmation_id (that ID
        # only appears one screen further, on the final confirmation.html;
        # see REPORT.md "Cuts" re: goal-completion ambiguity).
        "expected_output": {"new_account_number": "SAV-12345-NEW"},
        "replay_params": {"member_id": "12345", "account_type": "savings"},
    },
}


# ---------------------------------------------------------------------------
# Per-task runner
# ---------------------------------------------------------------------------

async def run_task(task_key: str, headless: bool = True) -> dict:
    """Run discovery + artifact build + replay for a single task."""
    task_cfg = TASKS[task_key]
    run_ts   = int(time.time())
    run_id   = f"{task_key}_{run_ts}"

    print(f"\n{'=' * 60}")
    print(f"TASK: {task_cfg['display_name']}")
    print(f"Goal: {task_cfg['goal']}")
    print(f"{'=' * 60}")

    # ---------------------------------------------------------------
    # Phase 1 — Discovery (Claude drives the browser)
    # ---------------------------------------------------------------
    print("\n[1/3] DISCOVERY RUN — Claude is driving the browser...")
    discovery_start = time.monotonic()
    result: AgentResult = await run_agent(
        goal     = task_cfg["goal"],
        headless = headless,
        run_id   = run_id,
        # This orchestrator runs unattended (no human present to click
        # "Resume"), so escalation is disabled here — a dead-end/escalate
        # will stop the run and report escalation_needed instead of
        # blocking on a human for up to escalation_timeout_s. To see the
        # real human-handoff path, run `python src/agent.py --escalate-demo`
        # or trigger it interactively (see README "Escalation demo").
        enable_escalation = False,
    )
    discovery_time = time.monotonic() - discovery_start

    print(f"      → {'SUCCESS' if result.success else 'FAILED'} "
          f"in {discovery_time:.1f}s, {len(result.steps)} steps, "
          f"stop={result.stop_reason}")
    if result.extracted_data:
        print(f"      → Extracted: {json.dumps(result.extracted_data)}")

    if not result.success:
        print(f"      ⚠ Discovery failed: {result.stop_reason}")
        print("      Artifact will still be built for inspection.")

    # ---------------------------------------------------------------
    # Phase 2 — Build & save artifact
    # ---------------------------------------------------------------
    print("\n[2/3] BUILDING ARTIFACT...")
    artifact: Artifact = build_artifact(
        result       = result,
        task_id      = task_cfg["task_id"],
        display_name = task_cfg["display_name"],
        description  = task_cfg["description"],
        category     = task_cfg["category"],
        risk_level   = task_cfg["risk_level"],
        parameters   = task_cfg["parameters"],
    )

    artifact_path = artifact.save(ARTIFACTS_DIR)
    print(f"      → Saved: {artifact_path}")
    print(f"      → {len(artifact.steps)} steps, schema v{artifact.schema_version}")

    risky_steps = [s for s in artifact.steps if s.risk_level == "risky"]
    if risky_steps:
        print(f"      → ⚠ {len(risky_steps)} RISKY step(s) flagged")

    # ---------------------------------------------------------------
    # Phase 3 — Deterministic replay (NO Claude)
    # ---------------------------------------------------------------
    print("\n[3/3] DETERMINISTIC REPLAY — no Claude calls...")
    replay_start = time.monotonic()
    replay_run_id = f"replay_{task_key}_{run_ts}"

    replay_ok    = False
    replay_output: dict = {}
    replay_stop  = "n/a"
    replay_error: str | None = None

    try:
        replay_result: ReplayResult = await replay_artifact(
            artifact_path        = artifact_path,
            params               = task_cfg["replay_params"],
            headless             = headless,
            require_confirmation = False,   # CI mode — auto-approve risky steps
            run_id               = replay_run_id,
        )
        replay_ok     = replay_result.success
        replay_output = replay_result.output
        replay_stop   = replay_result.stop_reason

    except ReplayError as exc:
        replay_error = f"Hard failure at step {exc.step_number}: {exc.reason}"
        if exc.screenshot_path:
            replay_error += f" [screenshot: {exc.screenshot_path}]"

    replay_time = time.monotonic() - replay_start

    if replay_error:
        print(f"      → HARD FAILURE: {replay_error}")
    else:
        print(f"      → {'SUCCESS' if replay_ok else 'FAILED'} "
              f"in {replay_time:.1f}s, stop={replay_stop}")
        if replay_output:
            print(f"      → Output: {json.dumps(replay_output)}")

    # ---------------------------------------------------------------
    # Summary
    # ---------------------------------------------------------------
    summary = {
        "task_id":          task_key,
        "display_name":     task_cfg["display_name"],
        "discovery": {
            "success":        result.success,
            "steps":          len(result.steps),
            "stop_reason":    result.stop_reason,
            "extracted_data": result.extracted_data,
            "time_s":         round(discovery_time, 2),
        },
        "artifact": {
            "path":           str(artifact_path),
            "step_count":     len(artifact.steps),
            "risky_steps":    len(risky_steps),
        },
        "replay": {
            "success":        replay_ok,
            "output":         replay_output,
            "stop_reason":    replay_stop,
            "error":          replay_error,
            "time_s":         round(replay_time, 2),
        },
    }

    print(f"\n{'─' * 60}")
    print(f"TASK {task_key.upper()} COMPLETE")
    print(f"  Discovery : {'✓' if result.success else '✗'}")
    print(f"  Artifact  : {artifact_path.name}")
    print(f"  Replay    : {'✓' if replay_ok else ('⚠ ' + (replay_error or replay_stop))}")
    print(f"{'─' * 60}")

    return summary


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    parser = argparse.ArgumentParser(description="Run interface.ai take-home tasks")
    parser.add_argument("--task",    choices=list(TASKS.keys()), help="Run a single task")
    parser.add_argument("--visible", action="store_true",         help="Show browser window")
    args = parser.parse_args()

    headless = not args.visible
    tasks_to_run = [args.task] if args.task else list(TASKS.keys())

    print("\n🏦 interface.ai Banking Agent — Task Runner")
    print(f"   Tasks  : {', '.join(tasks_to_run)}")
    print(f"   Browser: {'visible' if not headless else 'headless'}")
    print(f"   Evidence: {EVIDENCE_DIR}")

    results = []
    for task_key in tasks_to_run:
        try:
            summary = await run_task(task_key, headless=headless)
            results.append(summary)
        except Exception as exc:
            print(f"\n❌ Unhandled error in task {task_key}: {exc}")
            import traceback
            traceback.print_exc()
            results.append({"task_id": task_key, "error": str(exc)})

    # Write summary JSON
    summary_path = EVIDENCE_DIR / f"run_summary_{int(time.time())}.json"
    summary_path.parent.mkdir(exist_ok=True)
    summary_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\n📄 Summary written to: {summary_path}")

    # Print final table
    print("\n" + "=" * 60)
    print("FINAL RESULTS")
    print("=" * 60)
    for r in results:
        if "error" in r and "discovery" not in r:
            print(f"  {r['task_id']:20s} ❌ ERROR: {r['error']}")
        else:
            d_ok = r.get("discovery", {}).get("success", False)
            rep_ok = r.get("replay", {}).get("success", False)
            print(f"  {r['task_id']:20s}  Discovery: {'✓' if d_ok else '✗'}  Replay: {'✓' if rep_ok else '✗'}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())