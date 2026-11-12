"""Sub-agent adapters.

A sub-agent takes a step name and task dict, invokes some model, and returns
the structured output plus the exact prompt that produced it. The orchestrator
wraps every invocation in an invocation trace (see
:mod:`orchestrator.integrity`) so the gate can later verify the output was
not forged.

Three adapters ship:

- :class:`MockAgent` — deterministic, offline. Used by the demo CLI and tests.
- :class:`ClaudeCLIAdapter` — subprocess wrapper around Anthropic's ``claude``
  CLI with ``--output-format json``.
- :class:`CodexCLIAdapter` — subprocess wrapper around OpenAI's ``codex exec``
  CLI with ``--json`` output framing.

Both CLI adapters share a :class:`CLIAdapterBase` that owns prompt construction,
subprocess execution, timeout handling, and JSON parsing. A new provider only
needs to override the two methods that describe its command line and output
envelope.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Protocol

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


# ---------------------------------------------------------------------------
# Protocol + trace wrapper
# ---------------------------------------------------------------------------


class SubAgent(Protocol):
    """Protocol every sub-agent adapter must satisfy."""

    adapter_name: str
    model: str

    def run(self, step: str, task: dict) -> tuple[dict, str]:
        """Return ``(output_dict, prompt_sent)``.

        ``output_dict`` becomes the step payload (minus framework-managed
        keys); the trace commits to its ``extract_traceable_output``. The
        prompt is digested into ``prompt_digest`` on the trace.
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
        extra={"adapter": agent.adapter_name, "step": step, "has_trace": True},
    )
    return output


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------


_STEP_CONTRACTS: dict[str, str] = {
    "research": (
        'Return a JSON object: {"completed": true, '
        '"findings": [string, ...], "sources": [string, ...]}.'
    ),
    "draft": (
        'Return a JSON object: {"completed": true, '
        '"body": string, "word_count": integer}.'
    ),
    "review": (
        'Return a JSON object: {"completed": true, '
        '"approved": boolean, "comments": [string, ...]}.'
    ),
}


# JSON schemas for CLIs that support hard output constraints (e.g. Codex
# ``--output-schema``). Claude Code does not have a direct equivalent, so
# ClaudeCLIAdapter relies on ``_STEP_CONTRACTS`` embedded in the prompt only.
_STEP_SCHEMAS: dict[str, dict] = {
    "research": {
        "type": "object",
        "required": ["completed", "findings", "sources"],
        "properties": {
            "completed": {"type": "boolean"},
            "findings": {"type": "array", "items": {"type": "string"}},
            "sources": {"type": "array", "items": {"type": "string"}},
        },
        "additionalProperties": False,
    },
    "draft": {
        "type": "object",
        "required": ["completed", "body", "word_count"],
        "properties": {
            "completed": {"type": "boolean"},
            "body": {"type": "string"},
            "word_count": {"type": "integer"},
        },
        "additionalProperties": False,
    },
    "review": {
        "type": "object",
        "required": ["completed", "approved", "comments"],
        "properties": {
            "completed": {"type": "boolean"},
            "approved": {"type": "boolean"},
            "comments": {"type": "array", "items": {"type": "string"}},
        },
        "additionalProperties": False,
    },
}


def build_prompt(step: str, task: dict) -> str:
    """Return a step-appropriate prompt that pins the required output shape."""
    contract = _STEP_CONTRACTS.get(step)
    if contract is None:
        raise ValueError(f"no prompt contract for step {step!r}")
    summary = task.get("summary", "<no summary provided>")
    return (
        f"You are the '{step}' sub-agent in a multi-agent workflow.\n"
        f"Task: {summary}\n\n"
        f"Produce the output for this step.\n{contract}\n"
        "Return only the JSON object, no prose."
    )


def output_schema_for(step: str) -> dict:
    """Return the JSON schema that hard-constrains output for ``step``."""
    schema = _STEP_SCHEMAS.get(step)
    if schema is None:
        raise ValueError(f"no output schema for step {step!r}")
    return schema


# ---------------------------------------------------------------------------
# Mock
# ---------------------------------------------------------------------------


class MockAgent:
    """Deterministic, offline sub-agent used for tests and the demo CLI."""

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
                    "findings": [f"finding-{task_id}-1", f"finding-{task_id}-2"],
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


# ---------------------------------------------------------------------------
# CLI adapter base
# ---------------------------------------------------------------------------


class CLIAdapterError(RuntimeError):
    """Raised when the underlying CLI fails or returns malformed output."""


class CLIAdapterBase:
    """Shared execution path for subprocess-based sub-agent CLIs.

    Subclasses define three things:

    - ``adapter_name`` and ``model`` (class or instance attributes).
    - :meth:`build_command` — the argv list to run, including any flags.
    - :meth:`parse_envelope` — how to pull the structured output out of
      the CLI's stdout envelope.
    """

    adapter_name: str = "cli"

    def __init__(
        self,
        binary: str,
        *,
        model: str,
        timeout_seconds: int = 300,
    ) -> None:
        self.binary = binary
        self.model = model
        self.timeout_seconds = timeout_seconds

    def build_command(
        self, step: str, *, schema_path: str | None = None
    ) -> list[str]:  # pragma: no cover - abstract
        raise NotImplementedError

    def parse_envelope(self, stdout: str) -> dict:  # pragma: no cover - abstract
        raise NotImplementedError

    @contextmanager
    def _schema_path_for(self, step: str) -> Iterator[str | None]:
        """Override in subclasses that enforce output via a schema file.

        Base returns ``None`` — no hard enforcement, rely on the prompt.
        """
        yield None

    def run(self, step: str, task: dict) -> tuple[dict, str]:
        prompt = build_prompt(step, task)
        with self._schema_path_for(step) as schema_path:
            command = self.build_command(step, schema_path=schema_path)
            try:
                result = subprocess.run(
                    command,
                    input=prompt,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=self.timeout_seconds,
                )
            except subprocess.TimeoutExpired as error:
                raise CLIAdapterError(
                    f"{self.adapter_name} CLI timed out after {self.timeout_seconds}s"
                ) from error

        if result.returncode != 0:
            raise CLIAdapterError(
                f"{self.adapter_name} CLI exited {result.returncode}: "
                f"{result.stderr.strip() or result.stdout.strip() or '<no output>'}"
            )

        stdout = result.stdout.strip()
        if not stdout:
            raise CLIAdapterError(f"{self.adapter_name} CLI produced no stdout")

        output = self.parse_envelope(stdout)
        if not isinstance(output, dict):
            raise CLIAdapterError(
                f"{self.adapter_name} CLI output is not a JSON object: "
                f"got {type(output).__name__}"
            )
        return output, prompt


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------


class ClaudeCLIAdapter(CLIAdapterBase):
    """Wraps Anthropic's ``claude`` CLI invoked with ``--output-format json``.

    The CLI prints an envelope containing a ``result`` field whose value is
    a JSON string of the model's structured response.
    """

    adapter_name = "claude"

    def __init__(
        self,
        binary: str = "claude",
        *,
        model: str = "claude-sonnet-4-6",
        timeout_seconds: int = 300,
    ) -> None:
        super().__init__(binary, model=model, timeout_seconds=timeout_seconds)

    def build_command(self, step: str, *, schema_path: str | None = None) -> list[str]:
        # Claude Code has no direct --output-schema equivalent — we rely on
        # the per-step contract in the prompt. ``schema_path`` is accepted
        # for interface symmetry with CodexCLIAdapter but ignored.
        _ = schema_path
        return [
            self.binary,
            "-p",
            "--model",
            self.model,
            "--output-format",
            "json",
        ]

    def parse_envelope(self, stdout: str) -> dict:
        try:
            envelope: Any = json.loads(stdout)
        except json.JSONDecodeError as error:
            raise CLIAdapterError(
                "claude CLI stdout was not valid JSON"
            ) from error

        if isinstance(envelope, list):
            envelope = next(
                (item for item in envelope if isinstance(item, dict) and item.get("type") == "result"),
                None,
            )
            if envelope is None:
                raise CLIAdapterError("claude CLI event stream had no 'result' event")

        if not isinstance(envelope, dict):
            raise CLIAdapterError("claude CLI envelope was not a JSON object")

        if envelope.get("is_error"):
            raise CLIAdapterError(f"claude CLI reported an error: {envelope.get('result')}")

        result_text = envelope.get("result")
        if not isinstance(result_text, str) or not result_text.strip():
            raise CLIAdapterError("claude CLI envelope missing 'result' text")

        try:
            return json.loads(result_text)
        except json.JSONDecodeError as error:
            raise CLIAdapterError(
                "claude CLI result text was not valid JSON"
            ) from error


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------


class CodexCLIAdapter(CLIAdapterBase):
    """Wraps OpenAI's ``codex exec`` CLI invoked with ``--json``.

    The CLI emits either a single JSON object on stdout, or a newline-
    delimited event stream whose final event carries the structured output
    under ``last_message`` or ``result``. This adapter handles both shapes.
    """

    adapter_name = "codex"

    def __init__(
        self,
        binary: str = "codex",
        *,
        model: str = "gpt-5-codex",
        timeout_seconds: int = 300,
    ) -> None:
        super().__init__(binary, model=model, timeout_seconds=timeout_seconds)

    @contextmanager
    def _schema_path_for(self, step: str) -> Iterator[str | None]:
        """Materialise the step's JSON schema into a tempfile for --output-schema."""
        schema = output_schema_for(step)
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=True, encoding="utf-8"
        ) as handle:
            json.dump(schema, handle)
            handle.flush()
            yield handle.name

    def build_command(self, step: str, *, schema_path: str | None = None) -> list[str]:
        command = [
            self.binary,
            "exec",
            "--json",
            "--model",
            self.model,
        ]
        if schema_path:
            command.extend(["--output-schema", schema_path])
        return command

    def parse_envelope(self, stdout: str) -> dict:
        # Try one-shot JSON first. Accept only if it looks like a result
        # envelope (otherwise it may be an intermediate event; fall through
        # to the ndjson stream path).
        envelope: Any = None
        try:
            candidate = json.loads(stdout)
        except json.JSONDecodeError:
            candidate = None
        if isinstance(candidate, dict) and (
            "last_message" in candidate or "result" in candidate or "completed" in candidate
        ):
            envelope = candidate

        if envelope is None:
            # Fallback: newline-delimited event stream.
            events = []
            for line in stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            envelope = next(
                (
                    event
                    for event in reversed(events)
                    if isinstance(event, dict) and ("last_message" in event or "result" in event)
                ),
                None,
            )
            if envelope is None:
                raise CLIAdapterError("codex CLI stream had no result event")

        if not isinstance(envelope, dict):
            raise CLIAdapterError("codex CLI envelope was not a JSON object")

        payload: Any = envelope.get("last_message") or envelope.get("result") or envelope
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError as error:
                raise CLIAdapterError(
                    "codex CLI result text was not valid JSON"
                ) from error
        if not isinstance(payload, dict):
            raise CLIAdapterError(
                "codex CLI result payload was not a JSON object"
            )
        return payload


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_adapter(name: str) -> SubAgent:
    """Return an adapter instance for ``name``.

    ``mock`` is always safe; ``claude`` and ``codex`` require the respective
    binary to be installed and authenticated.
    """
    if name == "mock":
        return MockAgent()
    if name == "claude":
        return ClaudeCLIAdapter()
    if name == "codex":
        return CodexCLIAdapter()
    raise ValueError(f"unknown adapter: {name!r}")
