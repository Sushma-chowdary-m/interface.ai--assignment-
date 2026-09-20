# Computer-Use Automation System

This is an LLM-driven agent that figures out, by actually looking at the screen, how to complete
a task inside a legacy-style banking web portal. Once it succeeds, it saves what it did as a
typed, versioned **artifact** — a reusable capability — and from then on that same flow can be
**replayed deterministically, with no LLM in the loop**, with proper error handling and a real
path for a human to step in when something goes wrong.

The full design write-up (architecture, schema, determinism, multi-tenant story, escalation,
safety, and what I cut) is in [`REPORT.md`](REPORT.md). This file is just about getting it
running.

## 1. What's in here

```
src/
  agent.py       Discovery loop — Claude (vision) drives Playwright, observe → decide → act
  artifact.py    Artifact schema + builder (turns a discovery run into a typed, versioned JSON capability)
  replay.py      Deterministic replay engine — no LLM calls, tiered error handling
  guardrails.py  Allowlist, risk classification, PII redaction
  escalation.py  Human-in-the-loop handoff (pause / expose / capture / resume)
  api.py         FastAPI capability catalog — list/describe/invoke saved artifacts by name
  run_tasks.py   Orchestrator: discovery → build artifact → replay → compare, for both tasks
mock_bank/       The target app (Flask) — a stand-in for a legacy bank back-office system
artifacts/       Saved capability artifacts (JSON)
evidence/        Logs + screenshots from real discovery and replay runs
tests/           Unit tests for guardrails, replay error handling, and the artifact schema
```

## 2. Setting it up

You'll need Python 3.11+.

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium

cp .env.example .env
# edit .env and set ANTHROPIC_API_KEY — you need this for discovery runs
```

`.env` also holds `BANK_USERNAME` / `BANK_PASSWORD` for the mock portal. Both are read from the
environment at runtime only — they're never hardcoded anywhere in source, and they're kept out
of saved artifacts and logs (see the Safety section of REPORT.md for how, and for one place that
redaction wasn't actually working until I caught it and fixed it).

## 3. Demo path — the exact commands

**Terminal 1 — start the target app:**
```bash
python mock_bank/app.py
# serves http://127.0.0.1:5001
```

**Terminal 2 — run the whole pipeline (discovery → artifact → replay) for both tasks:**
```bash
python src/run_tasks.py
```
This one command is the whole vertical slice: a real Claude-driven discovery run against the
live mock bank, building the artifact, then replaying it deterministically — for both the
read-only task and the write/risky one. Logs and screenshots land in `evidence/<run_id>/`.

You can also run just one task, or watch it happen in a visible browser window instead of
headless:
```bash
python src/run_tasks.py --task read_balance
python src/run_tasks.py --visible
```

**Replay an existing artifact directly** — this is the path a production AI agent would
actually trigger: no LLM call, just deterministic execution against whatever parameters you
give it.
```bash
python src/replay.py artifacts/read_balance_v1_0.json member_id=482915
python src/replay.py artifacts/read_balance_v1_0.json member_id=000000  # business outcome: not found
```

**Run the discovery agent on its own**, against any goal you want to try:
```bash
python src/agent.py "Look up member 738204 and read their current savings balance"
```

**Escalation / human handoff demo** — this is a real handoff, not a mocked call (see REPORT.md
§5 for how it works under the hood):
```bash
python src/agent.py --escalate-demo "Look up member 482915 and read their current savings balance"
```
This forces the run to pause at step 2 and opens an oversight page at
`http://127.0.0.1:7777`. Click **Resume Automation** there when you're ready to hand control
back. The browser tab stays open the whole time — you're acting on the *same* session the agent
was using, not a fresh one, and whatever you do while it's paused gets folded back into that
run's evidence log before the agent picks back up.

**Capability API** — an agent-facing catalog that lists, describes, and invokes saved artifacts
by name:
```bash
uvicorn src.api:app --app-dir . --port 8000
curl http://127.0.0.1:8000/capabilities
curl http://127.0.0.1:8000/capabilities/read_balance
curl -X POST http://127.0.0.1:8000/capabilities/read_balance/run \
  -H "Content-Type: application/json" \
  -d '{"parameters": {"member_id": "482915"}}'
curl http://127.0.0.1:8000/runs/<run_id>          # poll status/result
```

## 4. Running the tests

```bash
pip install pytest pytest-asyncio   # already in requirements.txt
pytest -q
```
These are pure-logic unit tests — guardrails' risk/redaction rules, replay's business-outcome /
session-expiry / checkpoint / extraction logic, and artifact schema round-trips. No browser, no
API key, runs in under a second.

## 5. Running it without live services

- **No Anthropic API key?** You can still run `python src/replay.py <artifact> [params]`
  against anything already saved in `artifacts/` — replay never touches the model. You just
  can't run a fresh discovery.
- **No browser?** `pytest -q` covers all the decision logic (error tiers, guardrails, schema)
  without ever launching Playwright.
- **No mock bank running?** The capability API's `GET /capabilities` and
  `GET /capabilities/{id}` routes just read from `artifacts/*.json` on disk, so they work with
  no live target at all — only `POST .../run` actually needs the mock bank up.

## 6. What's mocked / known limitations

The full reasoning for each of these is in REPORT.md's Cuts section, but briefly:

- Login happens at the engine level (credentials pulled from env), not as an artifact-declared
  step. A real multi-tenant version would want auth to be its own reusable step type instead.
- The "operator console" for human handoff is a minimal single-page HTTP surface
  (`escalation.py`), not a real-time co-browsing UI — the assignment explicitly scopes that out.
  What's real is the handoff mechanism itself: the live session gets paused, a human can act on
  it, what they did gets captured, and the run resumes — none of that part is stubbed.
- Selector inference in `artifact.py` leans on a lookup table tuned to this one mock app's
  markup. A production version would want to pull ranked selector candidates from the
  accessibility tree instead, or have Claude propose them directly during discovery.
