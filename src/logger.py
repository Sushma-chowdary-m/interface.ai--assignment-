"""
src/logger.py
-------------
Structured JSON logger for the banking agent.

Every log entry is a JSON object written to:
  evidence/<run_id>/agent.log   (one JSON object per line — NDJSON)

Screenshots on failure are referenced by path in the log entry.
No PII or credentials are written — the guardrails redact() is applied to
every string value before writing.

Log levels: DEBUG | INFO | WARNING | ERROR | STEP | SCREENSHOT
"""

from __future__ import annotations

import json
import sys
import time
from enum import Enum
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from agent import AgentStep

from guardrails import GuardrailsEngine


class LogLevel(str, Enum):
    DEBUG      = "DEBUG"
    INFO       = "INFO"
    WARNING    = "WARNING"
    ERROR      = "ERROR"
    STEP       = "STEP"
    SCREENSHOT = "SCREENSHOT"


class AgentLogger:
    """
    Writes NDJSON logs to evidence/<run_id>/agent.log.
    Also echoes WARNING/ERROR to stderr for visibility.
    """

    def __init__(self, run_id: str, evidence_dir: Path):
        self.run_id  = run_id
        self._redact = GuardrailsEngine.redact

        # Per-run evidence directory
        self.run_dir = evidence_dir / run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.run_dir / "agent.log"

        # Open log file (append mode so re-runs accumulate)
        self._fh = self.log_path.open("a", encoding="utf-8")
        self.info("Logger initialised", {"log_path": str(self.log_path)})

    # ------------------------------------------------------------------
    # Core write
    # ------------------------------------------------------------------

    def _write(self, level: LogLevel, message: str, data: dict[str, Any]) -> None:
        entry = {
            "ts":      int(time.time() * 1000),
            "run_id":  self.run_id,
            "level":   level.value,
            "message": self._redact(message),
            "data":    GuardrailsEngine.sanitize_artifact(data),
        }
        line = json.dumps(entry, ensure_ascii=False)
        self._fh.write(line + "\n")
        self._fh.flush()

        # Echo warnings/errors to stderr
        if level in (LogLevel.WARNING, LogLevel.ERROR):
            print(f"[{level.value}] {message}", file=sys.stderr)

    # ------------------------------------------------------------------
    # Convenience methods
    # ------------------------------------------------------------------

    def debug(self, message: str, data: dict[str, Any] | None = None) -> None:
        self._write(LogLevel.DEBUG, message, data or {})

    def info(self, message: str, data: dict[str, Any] | None = None) -> None:
        self._write(LogLevel.INFO, message, data or {})

    def warning(self, message: str, data: dict[str, Any] | None = None) -> None:
        self._write(LogLevel.WARNING, message, data or {})

    def error(self, message: str, data: dict[str, Any] | None = None) -> None:
        self._write(LogLevel.ERROR, message, data or {})

    def screenshot(self, path: Path, context: str) -> None:
        self._write(LogLevel.SCREENSHOT, f"Screenshot saved: {context}", {
            "path":    str(path),
            "context": context,
        })

    def step(self, step: "AgentStep") -> None:
        """Log a completed agent step in structured form."""
        a = step.action
        self._write(LogLevel.STEP, f"Step {step.step_number}: {a.description}", {
            "step_number":     step.step_number,
            "timestamp_ms":    step.timestamp_ms,
            "page_url":        step.page_url,
            "page_title":      step.page_title,
            "action_kind":     a.kind.value,
            "action_selector": a.selector,
            "action_value":    a.value,       # passwords already blocked by guardrails
            "description":     a.description,
            "success":         a.success,
            "error":           a.error,
            "duration_ms":     a.duration_ms,
            "screenshot_path": a.screenshot_path,
            "extracted_data":  a.extracted_data,
        })

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        self._fh.close()

    def __del__(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Log reading helpers (used by API / reporting)
    # ------------------------------------------------------------------

    def read_entries(self) -> list[dict]:
        """Return all log entries as a list of dicts."""
        entries = []
        with self.log_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        return entries

    def steps_only(self) -> list[dict]:
        return [e for e in self.read_entries() if e.get("level") == "STEP"]

    def errors_only(self) -> list[dict]:
        return [e for e in self.read_entries() if e.get("level") in ("ERROR", "WARNING")]


# ---------------------------------------------------------------------------
# Module-level convenience: create a replay logger (no run dir, stdout only)
# ---------------------------------------------------------------------------

class ReplayLogger(AgentLogger):
    """
    Lightweight logger for deterministic replay runs.
    Writes to evidence/<run_id>/ just like the discovery logger.
    """
    def __init__(self, run_id: str, evidence_dir: Path):
        super().__init__(run_id=run_id, evidence_dir=evidence_dir)
        self.info("Replay logger started", {"replay_run_id": run_id})