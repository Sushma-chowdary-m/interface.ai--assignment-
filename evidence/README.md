# Evidence index

All logs are real runs against the live mock bank (`mock_bank/app.py`) and, for discovery,
a real Claude API call per step (vision-grounded, screenshots attached to each API request).
Nothing here is simulated or hand-written.

| Directory | What it shows |
|---|---|
| `read_balance_1789356524/` | **Discovery** — Claude drives the browser end-to-end for "look up member 482915 and read their savings balance." 11 steps, real screenshots before/after each action, goal met. Produced `artifacts/read_balance_v1_0.json`. |
| `open_account_1789870760/` | **Discovery** — Claude drives the "open a new savings sub-account" flow, including the risky open-account/confirm steps. 20 steps, goal met. Produced the current `artifacts/open_account_v1_0.json` (this is the re-run after fixing the `account_type` input-capture bug below, so the saved artifact actually reflects the fixed code). |
| `run_1789870517/` | **Escalation, actually triggered** — a real run of `python src/agent.py "..." --escalate-demo`, forced to escalate at step 2. Automation paused, brought up the real oversight page at `127.0.0.1:7777`, and blocked on the same live Playwright page. Resumed by POSTing to `/done` exactly the way the "Resume Automation" button does — the operator's notes are in the log. The agent then continued on the *same* session and reached `goal_met` with 20 total steps. |
| `replay_read_balance_1789356524/` | **Deterministic replay, success** — replays `read_balance_v1_0.json` with the same member recorded during discovery (482915). No LLM calls. Output: `{"savings_balance": "$4,250.00"}`. |
| `replay_read_balance_param_738204/` | **Deterministic replay, different parameter** — same artifact, `member_id=738204` (a member never seen during discovery). Proves parameterized replay actually substitutes the caller's value: output is Maria Garcia's real balance, `$12,750.00`. |
| `replay_read_balance_business_outcome/` | **Business outcome (Tier 1), the required error/exceptional-state case** — same artifact, `member_id=000000` (does not exist). Replay detects the app's real "No member found with ID: 000000" text and returns `success=true, stop_reason="business_outcome", business_outcome="member_not_found"` — a legitimate answer, not a crash. |
| `replay_open_account_1789357884/` | **Hard failure (Tier 3)** — an earlier replay of the write/risky artifact that ran into a genuine element-not-found after retries. Kept because it's real evidence of the third error tier (stop, screenshot, step number, reason) — see `1789358066923_hard_fail_step9.png`. |
| `replay_open_account_1789870760/` | **Deterministic replay of the current write/risky artifact, success** — replays the artifact produced by `open_account_1789870760/`. The RISKY confirm step goes through the confirmation gate. Output: `{"new_account_number": "SAV-482915-NEW"}`. |
| top-level `*.png` | Per-step screenshots (before and after each action), named `<timestamp>_step_NN[_post].png`. |

## Bugs this evidence trail actually surfaced (and the fixes)

This evidence isn't just a demo — it's what caught several real bugs during development,
each confirmed by re-running the exact same replay before and after the fix:

1. **Business-outcome detection false-positived on every replay** — scanning the whole page
   body caught a static "Test Member IDs" reference table's incidental wording. Fixed by
   scoping detection to the app's real error container (`.alert-error`).
2. **Parameterized replay was completely non-functional** — replaying with a different
   member ID returned the *original* member's data unchanged, because input resolution
   string-replaced a placeholder that was never actually stored in the recorded value.
3. **A `navigate` step with no recorded target silently reused `page_url`** (documentation-only
   discovery metadata) as if it were a real navigation instruction — teleporting a
   differently-parameterized replay back to the *original* member's page partway through the
   flow. This one hid behind bug #2: it only became visible once parameterization itself was
   fixed and a second member ID could be tested end-to-end.
4. **A button-selector fallback (`text=SEARCH`) was ambiguous** — it also matched an unrelated
   nav link ("Search Member") containing the same substring, and `.first` silently clicked the
   wrong element. Fixed by scoping the fallback to the ARIA `button` role instead of plain text.
5. **The login password was leaking into `agent.log` in plaintext.** `sanitize_artifact()` only
   redacts a value sitting next to a `"name": "password"` sibling key — the shape artifacts use.
   The per-step log entry has no such sibling (it's a flat `{selector, value}` pair), so the raw
   password was written straight into every discovery run's log. Fixed in `logger.py` by
   redacting any `type` action whose selector names a password field, independent of the value's
   content. Old logs that had already captured the plaintext value were scrubbed by hand; new
   runs (e.g. `open_account_1789870760/`) confirm the fix — see `"action_value": "[REDACTED]"`.
6. **The `open_account` artifact's `account_type` parameter had no step that consumed it** —
   `artifact.py` only captured an `InputParameter` for `type` and `navigate` actions, so the
   `select` step that actually chooses the account type recorded nothing. Replaying with a
   different `account_type` silently fell back to the discovery-time default instead of erroring
   or applying the caller's value. Fixed by giving `select` actions their own input-capture
   branch; `open_account_1789870760/` is the re-run that proves the fix (step 16 now carries
   `{"name": "account_type", "value": "savings", "is_templated": true}`).

See `REPORT.md` §3 for the full error-model writeup and §6 for the safety-related findings
(credential redaction, risk misclassification) found the same way — by running the system and
checking the actual output, not by reading the code.
