# REPORT — Computer-Use Automation System

## 1. Architecture

The system is a single Python process with five modules, deliberately not split into services:

```
goal ──▶ agent.py (discovery loop) ──▶ artifact.py (builder) ──▶ artifacts/*.json
                                                                        │
                                              replay.py (deterministic) ◀┘ ── params
                                                     │
                                        api.py (FastAPI capability catalog, wraps replay.py)

guardrails.py   — consulted by both agent.py and replay.py before every action
escalation.py   — invoked by agent.py (and designed to be invoked by replay.py) on stuck states
logger.py       — structured NDJSON evidence, used by every module
```

**Why single-process, not services.** The brief explicitly discourages building scaling
infrastructure prematurely ("queues, clusters, multi-tenant plumbing... is not [rewarded]").
Discovery and replay are both fundamentally "one browser session executes one flow" —
there's no concurrency problem to solve yet. `api.py`'s in-memory run registry is the one
place I'd swap in a real queue + datastore first if this went to production (see Cuts).

**Why Claude sees screenshots, not the DOM.** The brief is explicit that the common case is no
clean DOM. I standardized the whole loop on vision-based grounding (`agent.py` sends only a
PNG, never HTML) specifically so the same decision-making approach still works against a
legacy server-rendered app, a frameset, or a desktop screenshot — the model never depends on
selector quality to *decide* what to click; only the *execution* step (Playwright) needs a
selector, and that's resolved separately per action, with multiple fallback strategies tried
in order. This is the seam described in §4 below.

**Why the discovery loop and the replay engine are separate modules, not one engine with an
"LLM optional" flag.** They have fundamentally different failure semantics: discovery is
*exploratory* and permitted to waste steps, retry blindly, or hit a dead end and stop; replay
must be *deterministic* and classify every failure into one of three tiers before deciding what
to do. Conflating them would mean the replay path inherits discovery's tolerance for ambiguity,
which is exactly the reliability property replay exists to provide.

**Trade-off taken knowingly:** `agent.py`'s in-loop retry/backoff is much thinner than
`replay.py`'s (a transient failure during discovery just consumes a step toward `MAX_STEPS`).
This is intentional — discovery only has to succeed once to produce a good artifact; replay has
to succeed reliably every time it's invoked in production. Investing retry sophistication in
the path that gets called thousands of times, not the one that gets called once per capability
authored, is the higher-leverage trade.

## 2. Artifact schema

The artifact (`artifact.py`) is modeled as a **capability contract**, not a step recording, because
that's literally what an AI agent needs to invoke it safely:

- `TaskMeta` — `task_id`, `display_name`, `description`, `category` (read/write/mixed),
  `risk_level`, `tenant_id`, and a typed `parameters` list. This is the part a calling agent
  reads *before* invoking anything — enough to decide whether this capability is the right tool
  and what arguments it needs.
- `ArtifactStep` — the unit of replay. Each step carries:
  - `LocatorStrategy` (`primary` + ordered `fallbacks`, optional `frame`) — never a single
    selector. A legacy app's markup can shift between a class name and an href pattern without
    the *flow* changing; ranked fallbacks absorb that without a re-recording.
  - `InputParameter` with `is_templated` / `template_key` — the discovery run's literal value
    (`"482915"`) is preserved for audit, but replay substitutes `{{member_id}}` from caller-
    supplied params. This is what makes an artifact **parameterized** rather than a fixed replay
    of one specific run.
  - `OutputExtraction` (source: text/attribute/url/title/regex) — declares *what the capability
    returns* and *how*, decoupled from the step that produced it, so the caller's contract
    doesn't need to know about page structure at all.
  - `SuccessCheckpoint` — an explicit assertion (`url_contains`, `title_contains`,
    `element_visible`, `element_text`) that the step actually landed where expected, rather
    than trusting that a `.click()` returning without an exception means it worked.
  - `risk_level` / `is_reversible` / `on_failure` — per-step, not per-artifact. A single "open
    account" capability can have nine safe navigation/read steps and one risky "confirm" step;
    flagging risk at the artifact level would either over-block the safe steps or under-flag the
    dangerous one.
- `schema_version` at the artifact root, plus a `notes` field carrying discovery-run provenance
  (duration, step count, stop reason) — informational, never consulted by replay, kept purely
  for human review and debugging (a reviewer or the calling agent's operator should be able to
  read the JSON and understand the capability without re-running discovery).

**Why not just a step list.** A step list answers "what did the agent click." A capability
contract answers "what do I need to give this, what will it give me back, how do I know it
worked, and what happens if it doesn't" — the four questions an agent actually needs answered
to invoke it unattended. `api.py`'s `/capabilities/{id}` endpoint is the proof: it returns the
full typed contract (parameters, outputs, risk per step, on_failure policy) without ever
loading a browser.

## 3. Determinism & error handling

Replay never calls the model — every decision is: try the primary locator, then each fallback
in order; wait up to `timeout_ms`; verify the step's checkpoints; extract declared outputs.
Same artifact + same params ⇒ same steps, same page interactions, same output shape, run after
run. Non-determinism enters only from the target app's real state (data changes, session
expiry) — which is exactly the class of thing the error taxonomy below exists to handle
explicitly rather than mask.

**Three-tier error model** (implemented as three distinct exception types in `replay.py`,
not a single generic `try/except`):

| Tier | Exception | Meaning | Response |
|---|---|---|---|
| 1 | `BusinessOutcome` | A legitimate, expected non-happy-path result (member not found, restricted account) | Return `success=True` with `stop_reason="business_outcome"` — this is data the caller needs, not a crash |
| 2 | `RecoverableError` / `SessionExpiredError` | Transient — element not yet rendered, dialog interrupted the flow, session timed out mid-run | Retry with backoff (generic case); **re-authenticate then retry** (session-expiry case — see below) |
| 3 | `ReplayError` | Retries exhausted, or a condition that has no defined recovery | Stop immediately, capture a screenshot, return step number + expected-vs-observed detail for debugging |

Business-outcome and session-expiry detection are checked against the **target app's actual
rendered text** (`BUSINESS_OUTCOME_PATTERNS` in `replay.py`), not generic guesses — an earlier
version of this detector used patterns like `"member not found"` and `"access denied"` that
looked reasonable but never matched the mock bank's real strings (`"No member found with ID:"`,
`"Permission denied. This account is restricted."`). Verified this by writing failing unit
tests against the actual app copy before fixing the patterns (`tests/test_replay_logic.py`) —
a plausible-looking regex that doesn't match production text is silently dead code, and the
only way to catch that is to test against the real strings, not just the intent.

**Session expiry is deliberately not folded into `BusinessOutcome`.** It looks similar (a page
condition to detect), but the correct response is different — a `SessionExpiredError` triggers a
dedicated recovery action (re-authenticate) before the retry attempt, rather than either
crashing or misreporting an expired session as if it were a valid business answer. This is one
of the runtime conditions the brief calls out by name ("session/timeout expiry") and it's
handled with an actual recovery action, not just detection.

**Four bugs caught by actually running the system, not by reading the code** (all now fixed,
covered by regression tests where the bug is pure logic — the last two only fully surfaced
once the first was fixed and a genuinely different parameter value could be tested end-to-end):

1. *Parameterized replay was completely non-functional.* The original `resolve()` did a
   string-replace looking for a literal `"{{member_id}}"` placeholder *inside* the recorded
   input value — but the recorded value is the discovery-time literal (`"482915"`), never the
   placeholder text, so the replace was always a silent no-op. Every replay repeated whatever
   value was typed during discovery regardless of what params the caller passed — the single
   most important promise of "record once, invoke with different arguments" was broken. Found
   by replaying with `member_id=738204` and getting member 482915's balance back. Fixed by
   resolving through `InputParameter.is_templated`/`.name` against the caller's `params` dict
   directly, not through string substitution on the wrong field
   (`tests/...` — see `_execute_step`'s `resolve_input`).
2. *Business-outcome detection false-positived on every single replay.* The detector scanned
   the whole page body; this app's search page renders a static "Test Member IDs" reference
   table containing the literal words "Permission Denied" and "Not Found" as tester
   documentation, present on every page load regardless of the actual result. Every replay —
   including fully successful ones — was misreported as `business_outcome: access_denied`.
   Found the same way: ran a real replay and got a nonsensical result for a known-good member.
   Fixed by scoping detection to the app's actual error container (`.alert-error`, verified
   present on every real error state in `login.html`/`search.html`/`error.html`) with no
   full-page fallback — a fallback would have silently reintroduced the same false positive.

3. *A `navigate` step with no recorded target silently reused `page_url` — discovery-run
   provenance metadata, documented elsewhere as "informational, never consulted by replay" —
   as if it were an actual navigation instruction.* This happened specifically for a `navigate`
   action whose real value (a `javascript:` scroll snippet, not a URL) was previously discarded
   entirely — `build_artifact` only ever captured a value for `type` actions. With no input
   recorded, replay fell back to `step.page_url`, which is fine as a no-op *until* a
   differently-parameterized replay is genuinely on a different page than the one recorded
   during discovery — at which point it silently teleports the browser back to the original
   discovery-time record, discarding all parameterized progress made so far. This is exactly why
   bug #1 masked it: with parameterization broken, every replay stayed on the recorded member
   anyway, so the stale `page_url` always "coincidentally" matched. It only became visible once
   parameterization was fixed and a second member ID could be tested through the *entire* flow.
   Fixed two places: `build_artifact` now captures a `navigate` action's real value (URL or
   `javascript:` snippet) as an input like any other action; `_execute_step`'s navigate branch no
   longer falls back to `page_url` at all — with no target, it now correctly does nothing rather
   than guess one.
4. *A button locator's fallback was ambiguous.* `artifact.py` infers a `text=<label>` fallback
   from a click step's description (e.g. "Click the SEARCH button" → `text=SEARCH`) when the
   recorded CSS selector doesn't pan out. But this app's navbar has a "Search Member" link, and
   Playwright's `text=` match is a case-insensitive substring — so `text=SEARCH` matched *both*
   the real submit button and the unrelated nav link, and `.first` silently clicked whichever
   rendered first in the DOM (the nav link), which just reloads the search page without
   submitting anything. Fixed by scoping the fallback to Playwright's `button` ARIA role
   (`get_by_role("button", name=...)`) instead of plain text — an anchor-styled nav link has role
   `link`, not `button`, so it's correctly excluded regardless of what text it contains.

All four are now regression-tested (`tests/test_replay_logic.py`, `tests/test_artifact_schema.py`)
against the real page copy and real artifact shapes, not sanitized fixtures — the whole point of
bugs #1 and #2 is that a plausible-looking implementation can be wrong in a way unit tests only
catch if they use the target app's actual strings, and bugs #3/#4 only surfaced by replaying the
same artifact twice with two different, genuinely valid parameter sets and diffing the outputs —
a single happy-path replay looked completely fine in both cases.

**Checkpoints as a safety net, not a hard gate.** A checkpoint mismatch is currently logged as a
warning and replay continues rather than raising `ReplayError` — chosen because a checkpoint
(`title_contains`, `url_contains`) recorded from one discovery run can be a slightly loose
approximation of "did this work" (e.g. dynamic page titles), and hard-failing on every mismatch
would make replay brittle to exactly the kind of superficial variation the brief says is *not*
the interesting failure mode here (stable UIs, not layout drift). The trade-off: a checkpoint
failure that *should* have been fatal currently only warns. Flagged in Cuts as the first thing
I'd tighten with more time (make `on_failure` on the checkpoint itself, not just the step,
configurable per-artifact).

## 4. Heterogeneity & multi-tenant

**Surface abstraction.** The seam is already drawn at the right place: `agent.py`'s decision
loop only ever consumes a screenshot and produces `{selector, value, description}` — it has no
DOM/accessibility-tree dependency. Extending to a legacy web app requires no change to the
*decision* logic at all, only to the *execution* layer (`_execute_action` in `agent.py`,
`_locate`/`_execute_step` in `replay.py`), which would grow additional locator dialects:
CSS/text/placeholder today, `frame`-qualified locators for framesets (the schema already has
`LocatorStrategy.frame` reserved for this), and accessibility-tree node IDs or OS-level
coordinates for a desktop app. The `LocatorStrategy` abstraction (primary + ranked fallbacks) is
surface-agnostic on purpose — nothing about "try strategy 1, then 2, then 3 in order" assumes a
browser. A desktop surface would supply an `AccessibilityLocatorStrategy` implementing the same
try-in-order contract against UI Automation / AT-SPI node paths instead of CSS.

**Multi-tenant reuse.** `TaskMeta.tenant_id` exists today but is a label, not a mechanism —
that's the honest gap. The design I'd build next: keep one **base artifact** per vendor-product
capability (recorded once against a reference tenant) plus a small **override document per
tenant** — `{tenant_id, overrides: [{step_number, locator_patch, base_url_patch}]}` — merged
onto the base artifact at load time, the same pattern config layering tools use for
environment-specific overrides. Two tenants running the same vendor product differ in branding,
CSS classes, and maybe field order, not in the underlying *flow* — so the base artifact's steps,
checkpoints, and output contract stay shared; only locators and the entry URL get
patched per tenant. This turns "rebuild per tenant" into "record once, patch a few selectors,"
directly answering the brief's framing.

**Drift detection.** Not built (correctly out of scope per §3.7 — "design, not necessarily
build"), but the mechanism already exists half-way: replay logs a `checkpoint mismatch` warning
today without acting on it. The next step is trivial to describe precisely because of that: turn
"warn and continue" into "warn, and if the *fallback* locator succeeded while the *primary*
failed N times running, flag the artifact for review" — i.e., use the fallback-hit-rate signal
already produced by `_locate()`'s try-in-order loop as a drift detector, no new instrumentation
needed. Combined with the stretch-goal-shaped idea of a per-artifact confidence score (replay N
times, track success/flakiness), this gives an operator a concrete "this artifact needs
re-recording" signal instead of silent degradation.

## 5. Escalation & handoff

**Detecting stuck.** Two independent triggers: (1) the agent's own `action=escalate` when the
model itself judges it can't proceed safely, and (2) a mechanical dead-end detector
(`_is_dead_end` in `agent.py`) — the last 6 steps either all failed, or all hit the same URL
with the same action kind and no data extracted. Neither depends on the other; a model that
never says "I'm stuck" is still caught by the mechanical check.

**Taking control of the live session — actually real, not mocked.** `escalation.py`'s
`pause_for_human()` does not open a new session for the human: it keeps the *same* Playwright
`Page` (and therefore the same browser tab, cookies, and navigation state) open and interactive,
starts a small local HTTP oversight page (`http://127.0.0.1:7777`, no extra dependencies —
`http.server` in a background thread), and attaches Playwright's own `framenavigated`/`response`
event listeners to the page so every navigation and form submission the human makes is captured
as a `HumanAction` and folded into the run's evidence log. The human resumes by submitting a
form on the oversight page; that flips a `threading.Event` the async loop is polling, and
control returns to the agent, which re-observes the (now human-modified) page state and
continues. This is the exact mechanism the brief asks for: pause → expose the live session →
capture what the human did → signal resume → continue on the same session — not a fresh replay
of a mocked interaction. The **operator UI itself** is intentionally minimal (a single
auto-refreshing HTML page with a text box), which is exactly what §3.6 scopes as acceptable
("mock the operator UI... make the handoff mechanism and control-transfer model real").

**Wiring — a bug I caught and fixed while building this.** The escalation module originally
existed as working, independently-testable code but was never actually called from `agent.py`'s
main loop — the dead-end and explicit-escalate branches just set a flag and stopped. I wired
both trigger points to call `request_escalation()` for real: on dead-end or explicit escalate,
the loop now genuinely pauses on the live page, waits (bounded by `escalation_timeout_s`) for a
human, and — if resumed — appends the human's actions as a step in history and **continues the
same run** rather than terminating it. If the human doesn't respond before the timeout, the run
stops with `stop_reason="escalation_timeout"`, distinct from a plain `"escalated"` stop, so a
caller can tell "a human was asked but never showed up" from "a human handled it and declined
to resume."

**Unattended runs.** `run_tasks.py`'s orchestrator sets `enable_escalation=False` — there's no
human present in a CI-style batch run, so escalation there degrades to "stop and report
`escalation_needed`" rather than blocking for up to `escalation_timeout_s` with nobody watching.
`python src/agent.py --escalate-demo` exercises the real, blocking handoff path end-to-end for
evidence capture (see `evidence/` and README §3).

**Who's in control.** Single-writer by construction, not by a lock: while the human's oversight
page hasn't signaled resume, the agent loop is `await`-blocked inside `_handoff()` and issues no
further Playwright calls — there is no window where both the agent and a human are driving the
same page concurrently. A multi-operator version would need an explicit ownership token; not
needed here since there's exactly one automation process and one human per escalation.

## 6. Safety

Three layers in `guardrails.py`, all config-driven (regex lists, not code, so an operator can
tighten them without a deploy):

- **URL allowlist** — the agent may only visit origins matching `ALLOWED_URL_PATTERNS`, and even
  within an allowed origin, `BLOCKED_PATH_PATTERNS` (`/admin`, `/debug`, `/internal`) is checked
  separately and can't be bypassed by an otherwise-permitted origin.
- **Risk classification** — every action gets a `RiskLevel` (`safe` / `caution` / `risky` /
  `blocked`) from its kind plus selector/value heuristics (`submit`, `confirm`, `transfer`,
  `delete` upgrade a `caution` action to `risky`; `wire.transfer`, `admin` are unconditionally
  `blocked`). `risky` steps require explicit operator confirmation at replay time
  (`ReplayEngine._confirm_risky_step`) unless the caller passes `confirm_risky=true`
  (`api.py`) — deliberately conservative: an irreversible write never runs unattended without an
  explicit opt-in from whoever is invoking it.
- **Redaction** — `GuardrailsEngine.redact()` scrubs SSNs, card numbers, routing numbers, emails,
  and labeled password text from every log line, and `sanitize_artifact()` walks the artifact
  JSON recursively before it's ever written to disk. Neither is a single unconditional net,
  though — see the log-leak bug below, which is exactly a case where that assumption was wrong.

**A real risk-classification bug, also found by running the artifacts, not reading them.**
`RISKY_SELECTOR_PATTERNS` originally included a bare `r"submit"`. Nearly every HTML form
button is `type="submit"` — including a login button and a plain search button — so this
pattern flagged almost all form interactions as `risky` regardless of what they actually did.
Checking the two saved artifacts directly confirmed it: the login button, the search button,
*and* the genuinely irreversible "REVIEW & CONFIRM new account" button all resolved to the
identical selector `button[type='submit']`, so the classifier could not tell them apart and
flagged all three as risky — which would mean an operator has to approve a routine login and
a routine search exactly as often as an actual account-opening confirmation, training them to
stop reading the prompt (the precise failure mode a risk signal exists to prevent). Fixed two
ways: removed the bare `"submit"` pattern (it's HTML boilerplate, not a risk signal), and gave
`classify_risk`/`check` a third signal — the action's human-readable `description` (e.g. "Click
the REVIEW & CONFIRM button") — since the selector alone often can't distinguish a generic
submit button's actual purpose but the description reliably can. Re-classifying the two saved
artifacts with the fix correctly downgraded login/search to `caution` and, on the write
capability, correctly *promoted* the true "open sub-account" step to `risky` (it had been
under-flagged as `caution` before, for the same reason: its selector didn't happen to contain
the old bare-substring patterns either). Verified in `tests/test_guardrails.py`.

**A credential-redaction gap I found and fixed via testing, not inspection.** `InputParameter` and
`OutputExtraction` serialize as generic `{"name": ..., "value": ...}` pairs. The original
`sanitize_artifact()` only redacted a value when its own *key* was a blocked field name (e.g. a
top-level `"password"` key) — it never caught the case where the blocked name lives in a
sibling `"name"` field and the actual secret sits under the literal key `"value"`, which is
exactly the shape a discovery run produces when the agent types into a password field
(`InputParameter(name="password", value="REDACTED_PASSWORD")`). I found this with a unit test
(`test_sanitize_artifact_removes_blocked_fields_recursively`) that failed against the original
implementation, then fixed `sanitize_artifact` to also recognize and redact that name/value
pair shape. This is now covered by a regression test in `tests/test_artifact_schema.py`
(`test_password_input_is_redacted_on_save`) that asserts the *saved JSON file* never contains
the plaintext value.

**A real credential-leak bug — the password was reaching `agent.log` in plaintext.**
`sanitize_artifact()`'s name/value-pair redaction (described above) only fires when a value sits
next to a sibling `"name"` key that's a blocked field — the shape `InputParameter` and
`OutputExtraction` actually serialize as. `logger.py`'s per-step log entry has no such sibling:
it writes a flat `{"action_selector": ..., "action_value": ...}` pair, so a `type` action into
the password field wrote the real value straight into `agent.log`, no redaction applied at all.
I found this by grepping the actual discovery logs in `evidence/` for the literal password
string and getting real hits across six separate runs — not by reasoning about the code, since
the code's own inline comment at that line claimed the opposite ("passwords already blocked by
guardrails"). Fixed by redacting in `logger.py` directly: any `type` action whose selector names
a password field gets its value blanked before the entry is written, independent of what the
value actually is (so this doesn't quietly stop working the next time the demo password
rotates, the same reasoning behind the regex-based redaction above). Existing logs that had
already captured the plaintext value were scrubbed by hand; the re-run in
`evidence/open_account_1789870760/` confirms the fix going forward.

**Limits, stated plainly.** The allowlist and risk patterns are regex-based and could be evaded
by a sufficiently adversarial page (not a realistic threat model for an internal back-office
app the institution already trusts, but worth naming). Redaction is pattern-based, not a full DLP
system — a secret in a format not covered by `_PII_PATTERNS` would pass through. Risk
classification is per-action, not per-*session-of-actions* — a sequence of individually-safe
actions that composes into something risky (e.g. reading enough fields to reconstruct PII) isn't
caught. All three are named as next-step hardening in Cuts, not silently assumed away.

## 7. Cuts

What's built thin-but-real, and why:

- **Login is engine-level, not an artifact step.** `replay.py` authenticates before executing
  any artifact steps, reading `BANK_USERNAME`/`BANK_PASSWORD` from env. This was originally
  hardcoded literally in source (`"j.martinez"`/`"REDACTED_PASSWORD"` in both the system prompt and the
  replay engine) — fixed to env-sourced credentials, but the deeper limitation remains: auth
  isn't yet a first-class, composable artifact concept. **Next:** a reusable `authenticate` step
  type where the artifact declares a credential *reference* (e.g. `"credential_ref":
  "bank_portal_j_martinez"`), resolved from a secrets manager at replay time — this is what makes
  auth reusable across tenants running the same vendor product with different credentials,
  rather than baked into the engine for one app.
- **Locator inference is tuned to one app.** `artifact.py`'s `_clean_selector`/
  `_guess_selector_for_field` include a lookup table specific to the mock bank's markup.
  Documented rather than hidden. **Next:** rank candidate selectors from the accessibility tree
  at discovery time (more portable across legacy markup than guessing post-hoc from Claude's
  free-text selector suggestions).
- **Multi-tenant reuse is designed, not built** (per §3.7's explicit scoping) — see §4 above for
  the concrete override-document design I'd implement next.
- **The operator console is a single static-ish HTML page**, not a real-time co-browsing UI —
  explicitly scoped out by the brief (§3.6). The handoff *mechanism* underneath it is real.
- **Checkpoint mismatches warn rather than fail** (see §3) — the safer default given the brief's
  emphasis on stable UIs, but the first thing I'd make configurable per-artifact.
- **No confidence/stability scoring or multi-run flakiness signal** (stretch goal, not
  attempted) — the fallback-hit-rate data needed to build it is already produced by
  `_locate()`'s try-in-order loop, just not aggregated or persisted yet.
- **`api.py`'s run registry is in-memory** — fine for a demo, not for a restart-surviving
  production deployment. Would move to a real datastore before this became a second dependency
  any other service relied on.
- **A declared parameter wasn't backed by a step that consumed it — found and fixed.** The
  `open_account` artifact's `TaskMeta.parameters` lists `account_type` as a caller-supplied
  input, but `build_artifact` only ever created an `InputParameter` for `type` and `navigate`
  actions — a `select` step (choosing the account type from the dropdown) recorded nothing at
  all. Passing a different `account_type` at replay time was therefore silently a no-op. Fixed by
  giving `select` actions their own input-capture branch in `artifact.py`, named directly as
  `account_type` rather than inferred from the selector (the recorded selector for a `<select>`
  is generic — `"select"` — and carries no signal to infer a name from). Re-ran discovery to
  confirm: `evidence/open_account_1789870760/` produced an artifact whose step 16 now carries
  `{"name": "account_type", "value": "savings", "is_templated": true}`. **Still worth building
  next:** a build-time check that every declared parameter has at least one consuming step, so
  this class of gap becomes a build-time error instead of something that has to be found by
  re-verifying parameterization by hand.

**A reliability fix worth naming even though it's not a "bug" in the error-taxonomy sense:**
Claude's JSON action response occasionally includes trailing content after a complete, valid
object — closer to "the model kept talking" than malformed JSON. The original parser
(`json.loads(raw)`) requires the *entire* string to be exactly one JSON value, so it threw
`json.JSONDecodeError: Extra data` and burned a retry every time this happened — observed
directly across real discovery runs, more often on the longer 20-step `open_account` flow than
the short one, consistent with more conversation turns giving more chances for it to occur.
Switched to `json.JSONDecoder().raw_decode(raw)`, which parses one JSON value starting at
position 0 and ignores whatever follows — the correct tolerance here, since exactly one action
object is all this loop ever wants regardless of what else the model appended.

If I had another day, in priority order: (1) the `authenticate` step type, since it's the
biggest unlock for the multi-tenant story being real instead of aspirational; (2) multi-run
stability scoring, since the data's already half-collected; (3) the cross-tenant override-file
demo end-to-end against a second reskinned copy of the mock app, to make §4 executable proof
instead of design prose.
