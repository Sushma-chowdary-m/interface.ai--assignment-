# Computer-Use Automation System

An LLM-driven agent that discovers how to complete a task inside a legacy-style banking web
portal, records the discovered flow as a typed, versioned **artifact** (a reusable capability),
and replays that artifact **deterministically — without the LLM in the loop** — with structured
error handling and a real human-escalation path.

See [`REPORT.md`](REPORT.md) for the full design write-up (architecture, schema, determinism,
heterogeneity, escalation, safety, and cuts).

## 1. What's here

```
src/
  agent.py       Discovery loop — Claude (vision) drives Playwright, observe → decide → act
  artifact.py    Artifact schema + builder (AgentResult → typed, versioned JSON capability)
  replay.py      Deterministic replay engine — NO LLM calls, tiered error handling
  guardrails.py  Allowlist, risk classification, PII redaction
  escalation.py  Human-in-the-loop handoff (pause / expose / capture / resume)
  api.py         FastAPI capability catalog — list/describe/invoke artifacts by name
  run_tasks.py   Orchestrator: discovery → build artifact → replay → compare, for both tasks
mock_bank/       The target application (Flask) — stand-in for a legacy bank back-office app
artifacts/       Saved capability artifacts (JSON)
evidence/        Structured logs + screenshots from real discovery and replay runs
tests/           Unit tests for guardrails, replay error-tiering, and artifact schema
```

## 2. Setup

Requires Python 3.11+.

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium

cp .env.example .env
# then edit .env and set ANTHROPIC_API_KEY (required for discovery runs)
```

`.env` also carries `BANK_USERNAME` / `BANK_PASSWORD` for the mock portal — these are read from
env at runtime only, never hardcoded in source or written into artifacts/logs (see
`REPORT.md` → Safety).

## 3. Demo path (exact commands)

**Terminal 1 — start the target application:**
```bash
python mock_bank/app.py
# serves http://127.0.0.1:5001
```

**Terminal 2 — run the full pipeline (discovery → artifact → replay) for both tasks:**
```bash
python src/run_tasks.py
```
This is the single command that exercises the whole vertical slice: a real Claude-driven
discovery run against the live mock bank, artifact construction, and a deterministic replay of
the saved artifact — for both the read-only task and the write/risky task. Logs and screenshots
land in `evidence/<run_id>/`.

Run a single task, or watch it in a visible browser:
```bash
python src/run_tasks.py --task read_balance
python src/run_tasks.py --visible
```

**Replay an existing artifact directly** (this is the path a production AI agent would trigger
— no LLM call, just deterministic execution against saved parameters):
```bash
python src/replay.py artifacts/read_balance_v1_0.json member_id=12345
python src/replay.py artifacts/read_balance_v1_0.json member_id=00000   # business outcome: not found
```

**Run the discovery agent standalone** on an arbitrary goal:
```bash
python src/agent.py "Look up member 67890 and read their current savings balance"
```

**Escalation / human handoff demo** (real handoff, not mocked logic — see REPORT.md §5):
```bash
python src/agent.py --escalate-demo "Look up member 12345 and read their current savings balance"
```
This pauses the real agent run at step 2, opens an oversight page at
`http://127.0.0.1:7777`, and waits for you to click **Resume Automation**. The browser tab
stays open and interactive the whole time — you're operating the *same* session the agent was
using, not a fresh one. Whatever you do while paused is recorded and folded back into the run's
evidence log; the agent then re-observes the page and continues.

**Capability API** (agent-facing catalog — list/describe/invoke saved artifacts by name):
```bash
uvicorn src.api:app --app-dir . --port 8000
curl http://127.0.0.1:8000/capabilities
curl http://127.0.0.1:8000/capabilities/read_balance
curl -X POST http://127.0.0.1:8000/capabilities/read_balance/run \
  -H "Content-Type: application/json" \
  -d '{"parameters": {"member_id": "12345"}}'
curl http://127.0.0.1:8000/runs/<run_id>          # poll status/result
```

## 4. Running the tests

```bash
pip install pytest pytest-asyncio   # already in requirements.txt
pytest -q
```
Tests are pure-logic unit tests (guardrails' risk/redaction rules, replay's business-outcome /
session-expiry / checkpoint / extraction logic, artifact schema round-trips) — no browser or
API key required, run in well under a second.

## 5. Running without live services

- **No Anthropic API key**: you can still run `python src/replay.py <artifact> [params]` against
  any saved artifact in `artifacts/` — replay never calls the model. You just can't run new
  discovery.
- **No browser**: `pytest -q` covers all the decision logic (error tiering, guardrails,
  schema) without launching Playwright at all.
- **No mock bank running**: the capability API's `GET /capabilities` and
  `GET /capabilities/{id}` routes just read from `artifacts/*.json` on disk and work with no
  live target — only `POST .../run` needs the mock bank up.

## 6. Known limitations / what's mocked

Documented in full in `REPORT.md` → **Cuts**, but briefly:
- The target app's login is engine-level session setup (credentials from env), not an
  artifact-declared step — a real multi-tenant version would model auth as its own reusable,
  composable step type.
- The operator "console" for human handoff is a minimal single-page HTTP surface
  (`escalation.py`), not a real-time co-browsing UI — the brief explicitly scopes this out.
  The handoff *mechanism* (pause the live session, let a human act on it, capture what they
  did, resume) is real, not stubbed.
- Locator/selector inference in `artifact.py` includes a lookup table tuned to this one mock
  app's markup — a production version would source ranked selector candidates from the
  accessibility tree and/or have Claude emit them directly during discovery.
