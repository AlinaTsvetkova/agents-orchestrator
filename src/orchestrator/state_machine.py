"""Workflow state machine.

Five sequential steps, one human gate (``plan``), three sub-agent gates
(``research``, ``draft``, ``review``), one terminal step (``finalize``).

Each step has:

- a planner that validates / enriches the payload and may require either a
  human decision or a sub-agent run
- a gate that decides whether the step is done, or what the next required
  action is

Sub-agent gates verify the step's invocation trace before accepting
``completed=true``. See :mod:`orchestrator.integrity`.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestrator.integrity import (
    TraceMismatch,
    extract_traceable_output,
    strict_mode_enabled,
    verify_trace,
)
from orchestrator.structured_log import get_logger

_log = get_logger(__name__)

STEP_SEQUENCE: tuple[str, ...] = ("plan", "research", "draft", "review", "finalize")

# Steps where the main-agent gate verifies a sub-agent invocation trace.
TRACED_STEPS = frozenset({"research", "draft", "review"})


# ---------------------------------------------------------------------------
# State container
# ---------------------------------------------------------------------------


@dataclass
class RunState:
    run_id: str
    task: dict
    payload: dict = field(default_factory=dict)
    results: dict = field(default_factory=dict)
    current_step: str | None = None
    status: str = "pending"  # pending | running | paused | completed | failed
    workflow_status: str = "pending"
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def new(cls, task: dict) -> "RunState":
        now = _now_iso()
        return cls(
            run_id=str(uuid.uuid4()),
            task=task,
            created_at=now,
            updated_at=now,
        )

    def to_json(self) -> dict:
        return {
            "run_id": self.run_id,
            "task": self.task,
            "payload": self.payload,
            "results": self.results,
            "current_step": self.current_step,
            "status": self.status,
            "workflow_status": self.workflow_status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_json(cls, data: dict) -> "RunState":
        return cls(**data)


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def save_state(state: RunState, runs_dir: Path) -> Path:
    runs_dir.mkdir(parents=True, exist_ok=True)
    state.updated_at = _now_iso()
    path = runs_dir / f"{state.run_id}.json"
    path.write_text(json.dumps(state.to_json(), indent=2) + "\n", encoding="utf-8")
    _log.info(
        "run_state_saved",
        extra={
            "run_id": state.run_id,
            "status": state.status,
            "workflow_status": state.workflow_status,
            "current_step": state.current_step,
        },
    )
    return path


def load_state(run_id: str, runs_dir: Path) -> RunState:
    path = runs_dir / f"{run_id}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return RunState.from_json(data)


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


def _lift_and_verify_trace(payload: dict, step: str) -> str | None:
    """Pull inline ``_sub_agent_trace`` into the authoritative map and verify.

    Returns a failure reason string, or ``None`` on success.
    """
    step_payload = payload.setdefault(step, {})
    traces = payload.setdefault("sub_agent_traces", {})
    inline = step_payload.pop("_sub_agent_trace", None)
    if step not in traces and inline is not None:
        traces[step] = inline

    override = step_payload.get("human_override_reason")
    try:
        verify_trace(
            step=step,
            trace=traces.get(step),
            output=extract_traceable_output(step_payload),
            human_override_reason=override if isinstance(override, str) else None,
        )
    except TraceMismatch as error:
        if strict_mode_enabled():
            _log.warning(
                "integrity_check_failed",
                extra={"step": step, "detail": str(error), "mode": "strict"},
            )
            return str(error)
        _log.warning(
            "integrity_check_downgraded",
            extra={"step": step, "detail": str(error), "mode": "soft"},
        )
        payload.setdefault("sub_agent_trace_warnings", {})[step] = str(error)
        return None
    _log.info("integrity_check_passed", extra={"step": step})
    return None


def check_gate(state: RunState, step: str) -> dict:
    """Return the gate result for ``step``.

    Shapes returned:
        {"status": "completed", ...}            — accept and advance
        {"status": "agent_action_required", ..} — stop, next action needed
        {"status": "human_action_required", ..} — stop, needs operator
    """
    payload = state.payload
    step_payload = payload.setdefault(step, {})

    if step == "plan":
        if step_payload.get("approved"):
            return {"status": "completed", "notes": step_payload.get("notes", [])}
        return {
            "status": "human_action_required",
            "reason": "plan requires human approval",
            "hint": "set payload.plan.approved=true and resume",
        }

    if step in TRACED_STEPS:
        if not step_payload.get("completed"):
            return {
                "status": "agent_action_required",
                "reason": f"{step}.completed is false",
                "hint": f"invoke a sub-agent for step {step!r}",
            }
        failure = _lift_and_verify_trace(payload, step)
        if failure is not None:
            return {
                "status": "agent_action_required",
                "reason": "integrity_check_failed",
                "detail": failure,
            }
        return {"status": "completed", "step_payload": step_payload}

    if step == "finalize":
        # Compose the final artifact from verified upstream outputs.
        draft = payload.get("draft", {})
        review = payload.get("review", {})
        payload["finalize"] = {
            "completed": True,
            "artifact": {
                "body": draft.get("body"),
                "approved": review.get("approved", False),
                "comments": review.get("comments", []),
            },
        }
        return {"status": "completed"}

    raise ValueError(f"unknown step: {step}")


# ---------------------------------------------------------------------------
# Handoff / status router
# ---------------------------------------------------------------------------


def determine_handoff(state: RunState, gate_result: dict, step: str) -> dict:
    status = gate_result.get("status")
    if status == "completed":
        next_step = _next_step(step)
        if next_step is None:
            return {"workflow_status": "completed", "next_action": None}
        return {
            "workflow_status": "running",
            "next_action": {"type": "execute_step", "step": next_step},
        }
    if status == "agent_action_required":
        return {
            "workflow_status": "agent_action_required",
            "next_action": {
                "type": "invoke_sub_agent",
                "step": step,
                "reason": gate_result.get("reason"),
                "detail": gate_result.get("detail"),
            },
        }
    if status == "human_action_required":
        return {
            "workflow_status": "human_action_required",
            "next_action": {
                "type": "await_human",
                "step": step,
                "reason": gate_result.get("reason"),
                "hint": gate_result.get("hint"),
            },
        }
    raise ValueError(f"unknown gate status: {status!r}")


def _next_step(current: str) -> str | None:
    try:
        idx = STEP_SEQUENCE.index(current)
    except ValueError:
        return None
    if idx + 1 >= len(STEP_SEQUENCE):
        return None
    return STEP_SEQUENCE[idx + 1]


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


def advance(state: RunState) -> dict:
    """Run one pass of the state machine from ``state.current_step``.

    Stops at the first gate that is not satisfied. The caller is expected
    to supply the next sub-agent output / human decision, persist, and
    call ``advance`` again.
    """
    step = state.current_step or STEP_SEQUENCE[0]
    state.current_step = step
    state.status = "running"

    gate_result = check_gate(state, step)
    state.results[step] = gate_result
    handoff = determine_handoff(state, gate_result, step)
    _log.info(
        "handoff_decision",
        extra={
            "run_id": state.run_id,
            "step": step,
            "workflow_status": handoff["workflow_status"],
            "next_action_type": (handoff.get("next_action") or {}).get("type"),
        },
    )

    state.workflow_status = handoff["workflow_status"]
    if handoff["workflow_status"] == "completed":
        state.status = "completed"
    elif handoff["workflow_status"] == "running":
        next_action = handoff["next_action"]
        assert next_action is not None
        state.current_step = next_action["step"]
        state.status = "running"
    else:
        state.status = "paused"

    return handoff


def record_sub_agent_output(state: RunState, step: str, output: dict) -> None:
    """Merge a sub-agent output into the step's payload.

    Side-channel keys like ``_sub_agent_trace`` are preserved for the gate
    to lift and verify.
    """
    if step not in TRACED_STEPS:
        raise ValueError(f"step {step!r} does not accept sub-agent output")
    existing = state.payload.setdefault(step, {})
    existing.update(output)


def record_human_plan_approval(state: RunState, notes: list[str] | None = None) -> None:
    state.payload.setdefault("plan", {})
    state.payload["plan"]["approved"] = True
    if notes:
        state.payload["plan"]["notes"] = notes


class OverrideError(ValueError):
    """Raised when a human override request is rejected."""


def apply_human_override(
    state: RunState,
    step: str,
    reason: str,
    *,
    operator: str | None = None,
) -> dict:
    """Mark ``step`` complete under a human override, bypassing trace verify.

    - ``step`` must be a sub-agent gate (research / draft / review). The
      human plan gate has its own ``record_human_plan_approval`` helper,
      and ``finalize`` is deterministic.
    - ``reason`` must be a non-trivial string (≥ 12 non-whitespace chars) —
      a weak check against lazy ``"ok"``-style overrides.
    - The override is **also** appended to ``payload.overrides`` as an
      append-only audit list, so a later edit to ``payload.<step>`` cannot
      erase the fact that an override was applied.

    Returns the audit record that was appended.
    """
    if step not in TRACED_STEPS:
        raise OverrideError(
            f"override is only accepted for sub-agent gates ({sorted(TRACED_STEPS)}); "
            f"got {step!r}"
        )
    if not isinstance(reason, str) or len(reason.strip()) < 12:
        raise OverrideError(
            "override reason must be a string of at least 12 non-whitespace characters"
        )
    if state.status == "completed":
        raise OverrideError("cannot override: run is already completed")

    step_payload = state.payload.setdefault(step, {})
    step_payload["completed"] = True
    step_payload["human_override_reason"] = reason.strip()

    record = {
        "step": step,
        "reason": reason.strip(),
        "operator": operator,
        "applied_at": _now_iso(),
    }
    state.payload.setdefault("overrides", []).append(record)

    _log.warning(
        "human_override_applied",
        extra={
            "run_id": state.run_id,
            "step": step,
            "operator": operator,
            "reason_length": len(reason.strip()),
        },
    )
    return record
