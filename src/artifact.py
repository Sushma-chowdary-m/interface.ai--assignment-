"""
src/artifact.py
---------------
Artifact schema and builder.

After a successful discovery run the agent's AgentResult is converted into a
typed, versioned JSON artifact that:
  - captures every action with its locator strategy, inputs, and timing
  - records output extraction rules (what field → what selector / regex)
  - defines success checkpoints (URL pattern, page-title pattern, DOM presence)
  - carries metadata for versioning, tenancy, and replay compatibility

Schema version history
  1.0  —  initial release (this file)

The artifact is the contract between the discovery run and the replay engine.
replay.py reads it and re-executes deterministically WITHOUT calling Claude.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from guardrails import GuardrailsEngine

SCHEMA_VERSION = "1.0"
ARTIFACTS_DIR = Path(__file__).parent.parent / "artifacts"
ARTIFACTS_DIR.mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# Sub-schemas
# ---------------------------------------------------------------------------

@dataclass
class LocatorStrategy:
    """
    How to find an element on the page.
    Ordered list of strategies — replay tries each in sequence.
    """
    primary:   str                      # e.g. "text=Search Members"
    fallbacks: list[str] = field(default_factory=list)   # CSS / aria / placeholder
    frame:     str | None = None        # iframe selector if element is inside a frame


@dataclass
class InputParameter:
    """A value the agent types or selects at this step."""
    name:         str          # logical name, e.g. "member_id"
    value:        str          # the actual value used in the discovery run
    is_templated: bool = False # True → replay can substitute a different value
    template_key: str | None = None  # e.g. "{{member_id}}" for parameterised replay


@dataclass
class OutputExtraction:
    """
    How to extract a value from the page after this step succeeds.
    Replay reads these rules to assemble the final output dict.
    """
    field_name:    str         # key in the output dict, e.g. "savings_balance"
    source:        Literal["text", "attribute", "url", "title", "regex"]
    selector:      str | None  # CSS selector of the element to read
    attribute:     str | None  = None  # e.g. "value", "href" — only for source=attribute
    regex_pattern: str | None  = None  # capture group 1 used — only for source=regex
    regex_flags:   str         = "i"


@dataclass
class SuccessCheckpoint:
    """
    A condition that must be true for a step to be considered successful in replay.
    """
    kind:    Literal["url_contains", "title_contains", "element_visible", "element_text"]
    value:   str              # the URL fragment, title substring, selector, or expected text
    selector: str | None = None   # for element_visible / element_text


@dataclass
class ArtifactStep:
    """
    A single action in the artifact — the unit of replay.
    """
    step_number:   int
    action_kind:   str                  # navigate | click | type | select | wait | done
    description:   str
    locator:       LocatorStrategy | None
    inputs:        list[InputParameter]
    outputs:       list[OutputExtraction]
    checkpoints:   list[SuccessCheckpoint]
    risk_level:    str                  # safe | caution | risky
    is_reversible: bool
    timeout_ms:    int = 10_000
    retry_count:   int = 2
    on_failure:    Literal["stop", "retry", "skip", "escalate"] = "stop"

    # Discovery-run metadata (informational — not used by replay)
    duration_ms:     int = 0
    screenshot_path: str | None = None
    page_url:        str = ""
    page_title:      str = ""


@dataclass
class TaskMeta:
    """High-level metadata about the task this artifact encodes."""
    task_id:       str          # e.g. "read_balance"
    display_name:  str          # e.g. "Read Member Savings Balance"
    description:   str
    category:      Literal["read", "write", "mixed"]
    risk_level:    Literal["safe", "caution", "risky"]
    tenant_id:     str = "default"
    parameters:    list[dict]  = field(default_factory=list)  # typed parameter spec


@dataclass
class Artifact:
    """
    Root artifact schema.
    Serialised to JSON and saved to artifacts/<task_id>_<version>.json
    """
    schema_version:  str
    artifact_id:     str         # unique per discovery run
    created_at_ms:   int
    task:            TaskMeta
    entry_url:       str         # where replay starts
    steps:           list[ArtifactStep]
    expected_output: dict[str, Any]   # what a successful run should produce
    notes:           str = ""

    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        d = asdict(self)
        return GuardrailsEngine.sanitize_artifact(d)

    def save(self, directory: Path = ARTIFACTS_DIR) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        slug = self.task.task_id.replace(" ", "_").lower()
        fname = f"{slug}_v{self.schema_version.replace('.', '_')}.json"
        path = directory / fname
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path) -> "Artifact":
        raw = json.loads(path.read_text(encoding="utf-8"))
        return _deserialise(raw)


# ---------------------------------------------------------------------------
# Builder — converts AgentResult → Artifact
# ---------------------------------------------------------------------------

def build_artifact(
    result:       Any,   # AgentResult from agent.py
    task_id:      str,
    display_name: str,
    description:  str,
    category:     Literal["read", "write", "mixed"],
    risk_level:   Literal["safe", "caution", "risky"],
    parameters:   list[dict] | None = None,
    tenant_id:    str = "default",
) -> Artifact:
    """
    Convert an AgentResult into a replayable Artifact.

    The builder infers locator strategies, input parameters, and output
    extraction rules from the step data recorded during the discovery run.
    """
    from guardrails import GuardrailsEngine, RiskLevel
    _g = GuardrailsEngine()

    artifact_steps: list[ArtifactStep] = []

    for s in result.steps:
        a = s.action

        # Skip actions that failed during discovery — they never actually
        # advanced the page, so replaying them just chases stale state and
        # produces bogus checkpoint mismatches / hard failures downstream.
        if not a.success and a.kind.value not in ("done", "escalate"):
            continue

        # --- Locator ---
        locator: LocatorStrategy | None = None
        if a.selector:
            clean_selector = _clean_selector(a.selector)
            locator = LocatorStrategy(
                primary   = clean_selector,
                fallbacks = _infer_fallbacks(clean_selector, a.description),
            )

        # --- Inputs ---
        inputs: list[InputParameter] = []
        if a.kind.value == "type" and a.value:
            inputs.append(InputParameter(
                name         = _infer_input_name(a.selector or "field"),
                value        = a.value,
                is_templated = _is_templatable(a.selector or ""),
                template_key = _template_key(a.selector or ""),
            ))
        elif a.kind.value == "navigate" and a.value:
            # A navigate action's actual target (a URL, or a `javascript:`
            # scroll snippet the agent used for scrolling) was previously
            # discarded entirely during artifact building — only `type`
            # actions got an InputParameter. That meant replay's navigate
            # branch always fell through to reusing `step.page_url` (a
            # purely informational field documented elsewhere as "never
            # used by replay") as if it were the actual target — which is
            # usually a harmless no-op, but is a real bug the moment the
            # recorded page_url is for a *different* record than the one
            # the current (differently-parameterized) replay is actually
            # on: it silently teleports the browser back to the discovery-
            # time URL, discarding all parameterized progress. Found by
            # replaying this exact artifact with a different member_id and
            # watching the output revert to the original member's data.
            inputs.append(InputParameter(
                name         = "url",
                value        = a.value,
                is_templated = False,
                template_key = None,
            ))

        # --- Output extraction (only on done steps or steps with extracted_data) ---
        outputs: list[OutputExtraction] = []
        if a.extracted_data:
            for field_name, raw_value in a.extracted_data.items():
                outputs.append(OutputExtraction(
                    field_name    = field_name,
                    source        = "text",
                    selector      = _guess_selector_for_field(field_name),
                    regex_pattern = _guess_regex_for_value(str(raw_value)),
                ))

        # --- Checkpoints ---
        checkpoints: list[SuccessCheckpoint] = []
        if s.page_url:
            checkpoints.append(SuccessCheckpoint(
                kind  = "url_contains",
                value = _url_fragment(s.page_url),
            ))
        if s.page_title:
            checkpoints.append(SuccessCheckpoint(
                kind  = "title_contains",
                value = s.page_title[:40],
            ))

        # --- Risk ---
        # description matters: a login button, a search button, and an
        # irreversible "confirm new account" button can all resolve to the
        # identical selector button[type='submit'] — see guardrails.py.
        risk  = _g.classify_risk(a.kind.value, a.selector, a.value, a.description)
        is_rev = risk not in (RiskLevel.RISKY, RiskLevel.BLOCKED)

        on_fail: Literal["stop", "retry", "skip", "escalate"] = "stop"
        if a.kind.value == "wait":
            on_fail = "skip"
        elif risk == RiskLevel.RISKY:
            on_fail = "escalate"
        elif a.kind.value == "navigate":
            on_fail = "retry"

        artifact_steps.append(ArtifactStep(
            step_number     = s.step_number,
            action_kind     = a.kind.value,
            description     = a.description,
            locator         = locator,
            inputs          = inputs,
            outputs         = outputs,
            checkpoints     = checkpoints,
            risk_level      = risk.value,
            is_reversible   = is_rev,
            timeout_ms      = max(a.duration_ms * 3, 10_000),   # 3× actual + floor
            retry_count     = 2,
            on_failure      = on_fail,
            duration_ms     = a.duration_ms,
            screenshot_path = a.screenshot_path,
            page_url        = s.page_url,
            page_title      = s.page_title,
        ))

    task = TaskMeta(
        task_id      = task_id,
        display_name = display_name,
        description  = description,
        category     = category,
        risk_level   = risk_level,
        tenant_id    = tenant_id,
        parameters   = parameters or [],
    )

    return Artifact(
        schema_version  = SCHEMA_VERSION,
        artifact_id     = f"{task_id}_{int(time.time())}",
        created_at_ms   = int(time.time() * 1000),
        task            = task,
        entry_url       = result.steps[0].page_url if result.steps else "http://127.0.0.1:5001",
        steps           = artifact_steps,
        expected_output = result.extracted_data,
        notes           = (
            f"Discovery run completed in {result.total_time_ms}ms "
            f"with {len(result.steps)} steps. "
            f"Stop reason: {result.stop_reason}."
        ),
    )


# ---------------------------------------------------------------------------
# Inference helpers
# ---------------------------------------------------------------------------
def _clean_selector(selector: str) -> str:
    """
    Claude sometimes returns comma-separated mixed selectors like
    'button.btn-primary, a.btn-primary, text=OPEN SUB-ACCOUNT'
    which Playwright cannot parse as CSS. Extract the first valid one.
    """
    if not selector:
        return selector

    # Known good mappings for banking portal pages
    known = {
        "open-account": "a[href*='open-account']",
        "open sub-account": "a[href*='open-account']",
        "search member": "a[href='/search']",
        "view details": "a[href*='/member/']",
        "sign in": "button[type='submit']",
    }
    sel_lower = selector.lower()
    for key, replacement in known.items():
        if key in sel_lower:
            return replacement

    # If comma-separated, take the first part that is pure CSS (no text=)
    if "," in selector:
        parts = [p.strip() for p in selector.split(",")]
        for part in parts:
            if not part.startswith("text=") and "=" not in part:
                return part
        # All have =, just take first part
        return parts[0]

    return selector


_BUTTON_TEXT_RE = re.compile(r"\bthe\s+([A-Za-z0-9 &'/-]+?)\s+button\b", re.IGNORECASE)


def _infer_fallbacks(primary: str, description: str = "") -> list[str]:
    """
    Generate fallback selectors from a primary selector (and, for click
    steps, the action's description).

    A CSS class Claude guesses for a button (e.g. "button.search-btn") is
    frequently just wrong — it doesn't correspond to any real class in the
    target markup — and unlike the text=/placeholder=/select cases below,
    there is no generic prefix to pattern-match on to know a fallback is
    needed. That gap caused a real hard failure on replay: a discovery run
    recorded `button.search-btn` (real markup is `button[type='submit']`
    with class `btn-primary`) with zero fallbacks, so replay had no
    recovery path when the guessed class didn't exist. Caught by actually
    replaying the artifact, not by reading the code.

    The fix: Claude's action descriptions consistently follow "Click the
    <LABEL> button" phrasing (a property of how it's prompted, not
    specific to this one step) — extracting that visible label and adding
    it as a `text=` fallback works regardless of what CSS class the real
    button actually has, plus a generic `button[type='submit']` catch-all
    since every actionable button in this app (and most simple forms) is
    exactly one per visible form/section.
    """
    if primary.startswith("text="):
        return [
            "a[href='/search']",
            "a.btn-primary",
            "a[href*='open-account']",
        ]
    elif primary.startswith("placeholder="):
        return [
            "input[name='member_id']",
            "input[type='text']",
        ]
    elif "select" in primary.lower() or "account_type" in primary.lower():
        return [
            "select[name='account_type']",
            "#account_type",
        ]
    else:
        fallbacks: list[str] = []
        m = _BUTTON_TEXT_RE.search(description or "")
        if m:
            # role=button: (not text=) — a plain text match is ambiguous
            # whenever some other element's label contains the button's
            # label as a substring (e.g. a "Search" button vs. a "Search
            # Member" nav link); scoping to the button ARIA role rules
            # those false matches out. See replay.py's _get_loc.
            fallbacks.append(f"role=button:{m.group(1).strip()}")
        if "button" in primary.lower() or "btn" in primary.lower():
            fallbacks.append("button[type='submit']")
        return fallbacks


def _infer_input_name(selector: str) -> str:
    """Guess a logical name for an input from its selector."""
    mapping = {
        "member": "member_id",
        "account": "account_type",
        "search": "search_query",
        "username": "username",
        "password": "password",
    }
    sel_lower = selector.lower()
    for k, v in mapping.items():
        if k in sel_lower:
            return v
    return "input_value"


def _is_templatable(selector: str) -> bool:
    """True if this input could vary between replay runs (e.g. member ID)."""
    keys = ["member", "account", "search", "query", "id"]
    return any(k in selector.lower() for k in keys)


def _template_key(selector: str) -> str | None:
    if "member" in selector.lower():
        return "{{member_id}}"
    if "account" in selector.lower():
        return "{{account_type}}"
    return None


def _guess_selector_for_field(field_name: str) -> str | None:
    # Verified against the mock bank's actual rendered markup (not guessed).
    # member_detail.html and confirmation.html give stable element IDs for
    # their key fields. open_account.html's inline confirmation-preview
    # table (the page the discovery run actually stopped on — see
    # REPORT.md "Cuts" re: goal ambiguity around "reach the confirmation
    # screen") has NO stable IDs or classes on its data cells at all — a
    # deliberately-included legacy-markup case. For that page,
    # `tr:has-text(...)` (a Playwright selector-engine extension, not
    # plain CSS) targets the labeled row by its visible text instead of
    # position, which survives row reordering the way an nth-child
    # selector wouldn't — the same kind of pragmatic choice this system
    # would need repeatedly against a real no-test-ID legacy app.
    mapping = {
        "savings_balance":     "#savings-balance, .balance-amount",
        "checking_balance":    "#checking-balance, .balance-amount",
        "confirmation_id":     "#confirmation-id, .confirmation-id",
        "new_account_number":  "#new-account-number, tr:has-text('New Account Number') td:nth-child(2)",
        "account_number":      "#new-account-number, tr:has-text('New Account Number') td:nth-child(2)",
        "member_name":         ".member-name, h1.name",
        "member_status":       ".member-status, .status-badge",
    }
    return mapping.get(field_name)


def _guess_regex_for_value(value: str) -> str | None:
    import re
    if re.match(r"^\$[\d,]+\.\d{2}$", value):
        return r"\$[\d,]+\.\d{2}"
    if re.match(r"^CONF-\d+-[A-Z]+$", value):
        return r"CONF-\d+-[A-Z]+"
    if re.match(r"^[A-Z]+-\d+-[A-Z]+$", value):
        return r"[A-Z]+-\d+-[A-Z]+"
    return None


def _url_fragment(url: str) -> str:
    """Return the path portion of a URL for use as a checkpoint."""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    return path if path else "/"


# ---------------------------------------------------------------------------
# Deserialisation (dataclasses from raw dicts)
# ---------------------------------------------------------------------------

def _deserialise(raw: dict) -> Artifact:
    """Reconstruct an Artifact dataclass from a plain dict (loaded from JSON)."""

    def _loc(d: dict | None) -> LocatorStrategy | None:
        if not d:
            return None
        return LocatorStrategy(**d)

    def _inp(d: dict) -> InputParameter:
        return InputParameter(**d)

    def _out(d: dict) -> OutputExtraction:
        return OutputExtraction(**d)

    def _chk(d: dict) -> SuccessCheckpoint:
        return SuccessCheckpoint(**d)

    def _step(d: dict) -> ArtifactStep:
        return ArtifactStep(
            step_number     = d["step_number"],
            action_kind     = d["action_kind"],
            description     = d["description"],
            locator         = _loc(d.get("locator")),
            inputs          = [_inp(i) for i in d.get("inputs", [])],
            outputs         = [_out(o) for o in d.get("outputs", [])],
            checkpoints     = [_chk(c) for c in d.get("checkpoints", [])],
            risk_level      = d.get("risk_level", "safe"),
            is_reversible   = d.get("is_reversible", True),
            timeout_ms      = d.get("timeout_ms", 10_000),
            retry_count     = d.get("retry_count", 2),
            on_failure      = d.get("on_failure", "stop"),
            duration_ms     = d.get("duration_ms", 0),
            screenshot_path = d.get("screenshot_path"),
            page_url        = d.get("page_url", ""),
            page_title      = d.get("page_title", ""),
        )

    task_raw = raw["task"]
    task = TaskMeta(
        task_id      = task_raw["task_id"],
        display_name = task_raw["display_name"],
        description  = task_raw["description"],
        category     = task_raw["category"],
        risk_level   = task_raw["risk_level"],
        tenant_id    = task_raw.get("tenant_id", "default"),
        parameters   = task_raw.get("parameters", []),
    )

    return Artifact(
        schema_version  = raw["schema_version"],
        artifact_id     = raw["artifact_id"],
        created_at_ms   = raw["created_at_ms"],
        task            = task,
        entry_url       = raw["entry_url"],
        steps           = [_step(s) for s in raw["steps"]],
        expected_output = raw.get("expected_output", {}),
        notes           = raw.get("notes", ""),
    )


# ---------------------------------------------------------------------------
# CLI: print schema of a saved artifact
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        p = Path(sys.argv[1])
        art = Artifact.load(p)
        print(f"Artifact: {art.artifact_id}  schema_v{art.schema_version}")
        print(f"Task: {art.task.display_name}  ({art.task.category} / {art.task.risk_level})")
        print(f"Steps: {len(art.steps)}")
        for s in art.steps:
            risk_flag = "⚠" if s.risk_level == "risky" else " "
            print(f"  {risk_flag} {s.step_number:2d}. [{s.action_kind:10s}] {s.description}")
        print(f"Expected output: {json.dumps(art.expected_output, indent=2)}")
    else:
        print("Usage: python artifact.py <path/to/artifact.json>")