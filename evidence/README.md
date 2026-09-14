# Evidence index

All logs are real runs against the live mock bank (`mock_bank/app.py`) and, for discovery,
a real Claude API call per step (vision-grounded, screenshots attached to each API request).
Nothing here is simulated or hand-written.

| Directory | What it shows |
|---|---|
| `read_balance_1789356524/` | **Discovery** — Claude drives the browser end-to-end for "look up member 482915 and read their savings balance." 11 steps, real screenshots before/after each action, goal met. Produced `artifacts/read_balance_v1_0.json`. |
| `open_account_1789357884/` | **Discovery** — Claude drives the "open a new savings sub-account" flow, including the risky open-account/confirm steps. 20 steps, goal met. Produced `artifacts/open_account_v1_0.json`. |
| `replay_read_balance_1789356524/` | **Deterministic replay, success** — replays `read_balance_v1_0.json` with the same member recorded during discovery (482915). No LLM calls. Output: `{"savings_balance": "$4,250.00"}`. |
| `replay_read_balance_param_738204/` | **Deterministic replay, different parameter** — same artifact, `member_id=738204` (a member never seen during discovery). Proves parameterized replay actually substitutes the caller's value: output is Maria Garcia's real balance, `$12,750.00`. |
| `replay_read_balance_business_outcome/` | **Business outcome (Tier 1), the required error/exceptional-state case** — same artifact, `member_id=000000` (does not exist). Replay detects the app's real "No member found with ID: 000000" text and returns `success=true, stop_reason="business_outcome", business_outcome="member_not_found"` — a legitimate answer, not a crash. |
| `replay_open_account_1789357884/` | **Deterministic replay of the write/risky capability** — replays `open_account_v1_0.json`. The RISKY confirm step goes through the confirmation gate (`Auto-approving RISKY step N` in CI mode; interactively this prompts a human instead — same flag `api.py` exposes as `confirm_risky`). Output: `{"new_account_number": "SAV-482915-NEW"}`. |
| top-level `*.png` | Per-step screenshots (before and after each action) for the two discovery runs above, named `<timestamp>_step_NN[_post].png`. |

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

See `REPORT.md` §3 for the full error-model writeup and §6 for the safety-related findings
(credential redaction, risk misclassification) found the same way — by running the system and
checking the actual output, not by reading the code.
