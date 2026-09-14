"""
Artifact schema round-trip tests: save() -> load() must reproduce an
equivalent Artifact, and sensitive fields must never survive serialization.
This is the contract replay.py depends on (Artifact.load()) and the one a
human reviewer/calling agent inspects, so a silent round-trip break here is
as bad as a replay bug.
"""

import json
from pathlib import Path

from artifact import (
    Artifact, ArtifactStep, InputParameter, LocatorStrategy,
    OutputExtraction, SuccessCheckpoint, TaskMeta,
)


def _sample_artifact() -> Artifact:
    task = TaskMeta(
        task_id="read_balance", display_name="Read Balance",
        description="test", category="read", risk_level="safe",
        parameters=[{"name": "member_id", "type": "string", "required": True}],
    )
    step = ArtifactStep(
        step_number=1, action_kind="type", description="type member id",
        locator=LocatorStrategy(primary="placeholder=Member ID", fallbacks=["input[name='member_id']"]),
        inputs=[InputParameter(name="member_id", value="12345", is_templated=True, template_key="{{member_id}}")],
        outputs=[OutputExtraction(field_name="savings_balance", source="text", selector=".balance-amount")],
        checkpoints=[SuccessCheckpoint(kind="url_contains", value="/member/12345")],
        risk_level="safe", is_reversible=True,
    )
    return Artifact(
        schema_version="1.0", artifact_id="read_balance_123", created_at_ms=123456,
        task=task, entry_url="http://127.0.0.1:5001/login", steps=[step],
        expected_output={"savings_balance": "$4,250.00"},
    )


def test_round_trip_preserves_structure(tmp_path: Path):
    art = _sample_artifact()
    saved_path = art.save(tmp_path)
    assert saved_path.exists()

    loaded = Artifact.load(saved_path)
    assert loaded.task.task_id == art.task.task_id
    assert loaded.entry_url == art.entry_url
    assert len(loaded.steps) == 1
    assert loaded.steps[0].locator.primary == "placeholder=Member ID"
    assert loaded.steps[0].inputs[0].template_key == "{{member_id}}"
    assert loaded.steps[0].outputs[0].field_name == "savings_balance"
    assert loaded.expected_output == {"savings_balance": "$4,250.00"}


def test_password_input_is_redacted_on_save(tmp_path: Path):
    task = TaskMeta(task_id="t", display_name="t", description="t", category="read", risk_level="safe")
    step = ArtifactStep(
        step_number=1, action_kind="type", description="type password",
        locator=LocatorStrategy(primary="input[name='password']"),
        inputs=[InputParameter(name="password", value="bank123")],
        outputs=[], checkpoints=[], risk_level="caution", is_reversible=True,
    )
    art = Artifact(
        schema_version="1.0", artifact_id="t_1", created_at_ms=1,
        task=task, entry_url="http://x", steps=[step], expected_output={},
    )
    path = art.save(tmp_path)
    raw = json.loads(path.read_text())
    assert raw["steps"][0]["inputs"][0]["value"] == "[REDACTED]"


def test_schema_version_is_present_and_stable(tmp_path: Path):
    art = _sample_artifact()
    path = art.save(tmp_path)
    raw = json.loads(path.read_text())
    assert raw["schema_version"] == "1.0"
