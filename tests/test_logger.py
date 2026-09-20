"""
Regression test for a real credential leak: AgentLogger.step() wrote a
`type` action's raw value straight into agent.log. sanitize_artifact()
only redacts a value sitting next to a sibling "name": "password" key
(the InputParameter/OutputExtraction shape) — a step log entry has no
such sibling, so a password typed during discovery was never caught and
landed in plaintext in every discovery run's log. See REPORT.md §6.
"""

import json
from pathlib import Path

from agent import AgentAction, AgentStep, ActionKind
from logger import AgentLogger


def test_password_field_value_is_redacted_in_step_log(tmp_path: Path):
    action = AgentAction(
        kind=ActionKind.TYPE, selector="input[name='password']",
        value="REDACTED_PASSWORD", description="Type the password",
        success=True,
    )
    step = AgentStep(
        step_number=4, timestamp_ms=1, action=action,
        page_url="http://127.0.0.1:5001/login", page_title="FirstBank Enterprise Portal",
    )

    logger = AgentLogger(run_id="test_run", evidence_dir=tmp_path)
    logger.step(step)
    logger.close()

    raw_log = (tmp_path / "test_run" / "agent.log").read_text()
    assert "REDACTED_PASSWORD" not in raw_log

    entries = [json.loads(line) for line in raw_log.splitlines() if line.strip()]
    step_entry = next(e for e in entries if e["level"] == "STEP")
    assert step_entry["data"]["action_value"] == "[REDACTED]"


def test_non_password_field_value_is_not_redacted(tmp_path: Path):
    action = AgentAction(
        kind=ActionKind.TYPE, selector="input[name='username']",
        value="j.martinez", description="Type the username",
        success=True,
    )
    step = AgentStep(
        step_number=2, timestamp_ms=1, action=action,
        page_url="http://127.0.0.1:5001/login", page_title="FirstBank Enterprise Portal",
    )

    logger = AgentLogger(run_id="test_run2", evidence_dir=tmp_path)
    logger.step(step)
    logger.close()

    entries = [
        json.loads(line)
        for line in (tmp_path / "test_run2" / "agent.log").read_text().splitlines()
        if line.strip()
    ]
    step_entry = next(e for e in entries if e["level"] == "STEP")
    assert step_entry["data"]["action_value"] == "j.martinez"
