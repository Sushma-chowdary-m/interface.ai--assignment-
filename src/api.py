"""
src/api.py
----------
Capability API — FastAPI server that exposes saved artifacts as callable endpoints.

Another AI agent (or any HTTP client) can:
  GET  /capabilities            → list all available capabilities
  GET  /capabilities/{task_id}  → describe one capability (schema + parameters)
  POST /capabilities/{task_id}/run → invoke with typed arguments (triggers replay)
  GET  /runs/{run_id}           → get the status / result of a run
  GET  /runs/{run_id}/log       → get structured log entries for a run
  GET  /health                  → liveness check

Every invocation is a deterministic replay — Claude is NOT called.
RISKY capabilities require an explicit "confirm_risky": true in the request body.

Run with:
  uvicorn api:app --port 8000 --reload
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()   # must run before importing replay/agent (they read env at import time)

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from artifact import Artifact, ARTIFACTS_DIR
from replay import ReplayEngine, ReplayResult, ReplayError
from logger import AgentLogger

EVIDENCE_DIR = Path(__file__).parent.parent / "evidence"

app = FastAPI(
    title="Banking Agent Capability API",
    description=(
        "Exposes AI-discovered banking capabilities as deterministic, callable endpoints. "
        "Each capability maps to a saved artifact that is replayed without LLM calls."
    ),
    version="1.0.0",
)

# In-memory run registry (replace with a DB for production)
_runs: dict[str, dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_all_artifacts() -> dict[str, Artifact]:
    """Load all JSON artifacts from the artifacts directory."""
    artifacts: dict[str, Artifact] = {}
    for path in sorted(ARTIFACTS_DIR.glob("*.json")):
        try:
            art = Artifact.load(path)
            artifacts[art.task.task_id] = art
        except Exception as exc:
            print(f"[WARNING] Could not load artifact {path.name}: {exc}")
    return artifacts


def _artifact_summary(art: Artifact) -> dict:
    return {
        "task_id":      art.task.task_id,
        "display_name": art.task.display_name,
        "description":  art.task.description,
        "category":     art.task.category,
        "risk_level":   art.task.risk_level,
        "step_count":   len(art.steps),
        "parameters":   art.task.parameters,
        "artifact_id":  art.artifact_id,
        "schema_version": art.schema_version,
    }


def _result_to_dict(result: ReplayResult) -> dict:
    return {
        "artifact_id":     result.artifact_id,
        "task_id":         result.task_id,
        "success":         result.success,
        "output":          result.output,
        "steps_run":       result.steps_run,
        "total_time_ms":   result.total_time_ms,
        "stop_reason":     result.stop_reason,
        "business_outcome": result.business_outcome,
        "error":           result.error,
        "error_step":      result.error_step,
        "screenshot_path": result.screenshot_path,
    }


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class RunRequest(BaseModel):
    parameters:     dict[str, str] = Field(default_factory=dict,
        description="Key-value pairs substituted into templated artifact steps")
    confirm_risky:  bool = Field(False,
        description="Set true to auto-approve risky (irreversible) steps")
    headless:       bool = Field(True,
        description="Run browser in headless mode")
    tenant_id:      str  = Field("default",
        description="Tenant identifier for multi-tenant isolation")


class RunResponse(BaseModel):
    run_id:     str
    task_id:    str
    status:     str    # "queued" | "running" | "done" | "failed"
    message:    str


class RunStatus(BaseModel):
    run_id:      str
    task_id:     str
    status:      str
    result:      dict | None = None
    queued_at_ms: int
    started_at_ms: int | None = None
    finished_at_ms: int | None = None


# ---------------------------------------------------------------------------
# Background task executor
# ---------------------------------------------------------------------------

async def _execute_run(run_id: str, artifact: Artifact, request: RunRequest) -> None:
    """Background task that runs replay and updates _runs registry."""
    _runs[run_id]["status"]       = "running"
    _runs[run_id]["started_at_ms"] = int(time.time() * 1000)

    try:
        async with ReplayEngine(
            headless             = request.headless,
            require_confirmation = not request.confirm_risky,
            run_id               = run_id,
        ) as engine:
            result = await engine.run(artifact, params=request.parameters)

        _runs[run_id]["status"]         = "done"
        _runs[run_id]["result"]         = _result_to_dict(result)
        _runs[run_id]["finished_at_ms"] = int(time.time() * 1000)

    except ReplayError as exc:
        _runs[run_id]["status"] = "failed"
        _runs[run_id]["result"] = {
            "error":           exc.reason,
            "error_step":      exc.step_number,
            "screenshot_path": exc.screenshot_path,
        }
        _runs[run_id]["finished_at_ms"] = int(time.time() * 1000)

    except Exception as exc:
        _runs[run_id]["status"] = "failed"
        _runs[run_id]["result"] = {"error": str(exc)}
        _runs[run_id]["finished_at_ms"] = int(time.time() * 1000)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    artifacts = _load_all_artifacts()
    return {
        "status":            "ok",
        "capabilities_count": len(artifacts),
        "timestamp_ms":      int(time.time() * 1000),
    }


@app.get("/capabilities")
async def list_capabilities():
    """List all discoverable capabilities (artifacts)."""
    artifacts = _load_all_artifacts()
    return {
        "capabilities": [_artifact_summary(a) for a in artifacts.values()],
        "count":        len(artifacts),
    }


@app.get("/capabilities/{task_id}")
async def get_capability(task_id: str):
    """Describe a single capability in detail, including its step schema."""
    artifacts = _load_all_artifacts()
    if task_id not in artifacts:
        raise HTTPException(status_code=404, detail=f"Capability '{task_id}' not found")

    art = artifacts[task_id]
    summary = _artifact_summary(art)

    # Add step details for introspection
    summary["steps"] = [
        {
            "step_number": s.step_number,
            "action_kind": s.action_kind,
            "description": s.description,
            "risk_level":  s.risk_level,
            "is_reversible": s.is_reversible,
            "inputs": [
                {
                    "name":         i.name,
                    "is_templated": i.is_templated,
                    "template_key": i.template_key,
                }
                for i in s.inputs
            ],
            "outputs": [
                {"field_name": o.field_name, "source": o.source}
                for o in s.outputs
            ],
            "on_failure": s.on_failure,
        }
        for s in art.steps
    ]
    summary["expected_output"] = art.expected_output
    summary["notes"] = art.notes

    return summary


@app.post("/capabilities/{task_id}/run", response_model=RunResponse)
async def invoke_capability(
    task_id: str,
    body: RunRequest,
    background_tasks: BackgroundTasks,
):
    """
    Invoke a capability by name. Triggers a deterministic replay.
    Returns a run_id for polling the result.
    """
    artifacts = _load_all_artifacts()
    if task_id not in artifacts:
        raise HTTPException(status_code=404, detail=f"Capability '{task_id}' not found")

    art = artifacts[task_id]

    # Safety: risky capabilities require explicit confirmation flag
    has_risky = any(s.risk_level == "risky" for s in art.steps)
    if has_risky and not body.confirm_risky:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Capability '{task_id}' contains RISKY (irreversible) steps. "
                "Set 'confirm_risky': true to proceed."
            ),
        )

    run_id = f"{task_id}_{uuid.uuid4().hex[:8]}"
    _runs[run_id] = {
        "run_id":       run_id,
        "task_id":      task_id,
        "status":       "queued",
        "result":       None,
        "queued_at_ms": int(time.time() * 1000),
        "started_at_ms": None,
        "finished_at_ms": None,
        "request":      body.model_dump(),
    }

    background_tasks.add_task(_execute_run, run_id, art, body)

    return RunResponse(
        run_id  = run_id,
        task_id = task_id,
        status  = "queued",
        message = f"Run queued. Poll GET /runs/{run_id} for status.",
    )


@app.get("/runs/{run_id}", response_model=RunStatus)
async def get_run_status(run_id: str):
    """Get the current status and result of a run."""
    if run_id not in _runs:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
    r = _runs[run_id]
    return RunStatus(
        run_id          = r["run_id"],
        task_id         = r["task_id"],
        status          = r["status"],
        result          = r.get("result"),
        queued_at_ms    = r["queued_at_ms"],
        started_at_ms   = r.get("started_at_ms"),
        finished_at_ms  = r.get("finished_at_ms"),
    )


@app.get("/runs/{run_id}/log")
async def get_run_log(run_id: str):
    """Return the structured log entries for a run."""
    log_path = EVIDENCE_DIR / run_id / "agent.log"
    if not log_path.exists():
        raise HTTPException(status_code=404, detail=f"Log for run '{run_id}' not found")

    entries = []
    with log_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return {"run_id": run_id, "entries": entries, "count": len(entries)}


@app.get("/runs")
async def list_runs(limit: int = 20):
    """List recent runs."""
    runs = sorted(_runs.values(), key=lambda r: r["queued_at_ms"], reverse=True)
    return {"runs": runs[:limit], "total": len(_runs)}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    from dotenv import load_dotenv
    load_dotenv()
    uvicorn.run("api:app", host="127.0.0.1", port=8000, reload=True)