# Evidence index

All logs are real runs against the live mock bank (`mock_bank/app.py`) and, for discovery,
a real Claude API call per step (vision-grounded, screenshots attached to each API request).
Nothing here is simulated or hand-written.

| Directory | What it shows |
|---|---|
| `read_balance_1789343548/` | **Discovery** — Claude drives the browser end-to-end for "look up member 12345 and read their savings balance." 11 steps, real screenshots before/after each action, goal met. Produced `artifacts/read_balance_v1_0.json`. |
| `open_account_1789343597/` | **Discovery** — Claude drives the "open a new savings sub-account" flow, including the two RISKY steps (open-account, review & confirm). 20 steps, goal met. Produced `artifacts/open_account_v1_0.json`. |
| `replay_1789345699/` | **Deterministic replay, success** — replays `read_balance_v1_0.json` with `member_id=12345` (the same member recorded during discovery). No LLM calls. Output: `{"savings_balance": "$4,250.00"}`. |
| `replay_1789345987/` | **Deterministic replay, different parameter** — same artifact, `member_id=67890` (a member never seen during discovery). Proves parameterized replay actually substitutes the caller's value rather than repeating the recorded one: output is Maria Garcia's real balance, `$12,750.00`. Two checkpoint-mismatch warnings are expected and non-fatal (the recorded checkpoints reference member 12345's URL/title). |
| `replay_1789346005/` | **Business outcome (Tier 1), the required error/exceptional-state case** — same artifact, `member_id=00000` (does not exist). Replay detects the app's real "No member found with ID: 00000" text and returns `success=true, stop_reason="business_outcome", business_outcome="member_not_found"` — a legitimate answer, not a crash. |
| `replay_open_account_1789343597_success/` | **Deterministic replay of the write/risky capability** — replays `open_account_v1_0.json`. Log shows the two RISKY steps being gated through the confirmation mechanism (`Auto-approving RISKY step N (CI mode)` — this is `require_confirmation=False`, the same flag `api.py` exposes as `confirm_risky` for programmatic callers; interactively this prompts a human instead). Output: `{"new_account_number": "SAV-12345-NEW"}`. |
| top-level `*.png` | Per-step screenshots (before and after each action) for the two discovery runs above, named `<timestamp>_step_NN[_post].png`. |

See `REPORT.md` §3 for the three-tier error model these runs exercise, and §6 for how a
real bug (business-outcome detection false-positiving on unrelated static page text) was
caught by inspecting one of these logs, not by code review alone.
