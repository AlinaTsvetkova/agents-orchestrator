"""Sub-agent adapters.

A sub-agent takes a task plus a step name and returns a result dict plus
the exact prompt that produced it. The orchestrator wraps every invocation
in an invocation trace (see :mod:`orchestrator.integrity`) so the main
gate can later verify the output was not forged.

``MockAgent`` is the default and only built-in adapter. ``ClaudeCLIAdapter``
is sketched as a reference for wrapping a real subprocess-based CLI agent.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from typing import Any, Protocol

from orchestrator.integrity import compute_trace, extract_traceable_output
from orchestrator.structured_log import get_logger

_log = get_logger(__name__)


def utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


class SubAgent(Protocol):
    """Protocol every sub-agent adapter must satisfy."""

    adapter_name: str
    model: str

    def run(self, step: str, task: dict) -> tuple[dict, str]:
        """Return ``(output_dict, prompt_sent)``.

        ``output_dict`` becomes the step payload, minus framework-managed
        keys; the trace commits to it. ``prompt_sent`` is digested into
        ``prompt_digest`` on the trace.
        """
        ...


def run_with_trace(agent: SubAgent, step: str, task: dict) -> dict:
    """Invoke ``agent`` and attach an invocation trace to its output.

    The returned dict carries ``_sub_agent_trace`` as a side-channel key.
    The orchestrator lifts this into ``payload.sub_agent_traces[step]``
    before accepting the step as complete.
    """
    _log.info(
        "agent_started",
        extra={"adapter": agent.adapter_name, "step": step, "model": agent.model},
    )
    output, prompt = agent.run(step, task)
    trace = compute_trace(
        step=step,
        adapter=agent.adapter_name,
        model=agent.model,
        session_id=None,
        prompt=prompt,
        output=extract_traceable_output(output),
        timestamp=utc_now_iso(),
    )
    output["_sub_agent_trace"] = trace
    _log.info(
        "agent_completed",
        extra={
            "adapter": agent.adapter_name,
            "step": step,
            "has_trace": True,
        },
    )
    return output


class MockAgent:
    """Deterministic, offline sub-agent used for tests and the demo CLI.

    The mock returns a plausible payload shape for each gate step so that
    the whole workflow can be exercised without any external API calls.
    """

    adapter_name = "mock"

    def __init__(self, model: str = "mock-v1") -> None:
        self.model = model

    def _stable_id(self, task: dict, step: str) -> str:
        blob = json.dumps({"task": task, "step": step}, sort_keys=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]

    def run(self, step: str, task: dict) -> tuple[dict, str]:
        task_id = self._stable_id(task, step)
        prompt = f"[mock prompt] step={step} task_id={task_id} summary={task.get('summary')!r}"
        if step == "research":
            return (
                {
                    "completed": True,
                    "findings": [
                        f"finding-{task_id}-1",
                        f"finding-{task_id}-2",
                    ],
                    "sources": [f"source://{task_id}"],
                },
                prompt,
            )
        if step == "draft":
            return (
                {
                    "completed": True,
                    "body": f"Draft for {task.get('summary')} (id={task_id}).",
                    "word_count": 8,
                },
                prompt,
            )
        if step == "review":
            return (
                {
                    "completed": True,
                    "approved": True,
                    "comments": [f"Looks good on pass {task_id}."],
                },
                prompt,
            )
        raise ValueError(f"mock agent has no behavior for step {step!r}")


class ClaudeCLIAdapter:
    """Example subprocess adapter around a hypothetical ``claude`` CLI.

    Not wired into the default CLI — kept as a reference for integrators.
    """

    adapter_name = "claude"

    def __init__(self, binary: str = "claude", model: str = "claude-sonnet-4-6") -> None:
        self.binary = binary
        self.model = model

    def run(self, step: str, task: dict) -> tuple[dict, str]:
        prompt = json.dumps({"step": step, "task": task}, sort_keys=True)
        result = subprocess.run(
            [self.binary, "--model", self.model, "--json"],
            input=prompt,
            text=True,
            capture_output=True,
            check=True,
            timeout=300,
        )
        return json.loads(result.stdout), prompt
