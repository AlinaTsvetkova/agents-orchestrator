"""Sub-agent invocation integrity.

A *trace* is a small record that a sub-agent adapter produces for every
invocation. It commits the adapter to the exact output it returned, so the
orchestrator can verify at each gate that the output in the run state was
not forged or edited by anything other than the sub-agent itself.

Two digests are recorded per trace:

- ``output_digest`` is **authoritative** — HMAC-SHA256 if
  ``ORCHESTRATOR_TRACE_SECRET`` is set in the environment (true forgery
  protection), otherwise SHA-256 (tamper-evident against accidental edits
  only). Verified at each main-agent gate.
- ``prompt_digest`` is informational SHA-256 of the prompt the adapter
  sent. Not re-verified at runtime; present so an operator can reproduce
  the prompt and detect prompt-swap tampering during an investigation.

Mode flags:

- ``ORCHESTRATOR_TRACE_SECRET``: when set, enables HMAC. Keep this secret
  out of the lead agent's reach so it cannot re-sign forged traces.
- ``ORCHESTRATOR_STRICT_INTEGRITY``: defaults on. Set to ``0`` to downgrade
  trace mismatches to warnings — intended for migrating legacy runs, not
  for production.

A step payload may also carry ``human_override_reason`` (non-empty string)
to let a human take manual ownership of a gate without a sub-agent run.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Any

TRACE_SECRET_ENV = "ORCHESTRATOR_TRACE_SECRET"
STRICT_MODE_ENV = "ORCHESTRATOR_STRICT_INTEGRITY"
TRACE_SCHEMA_VERSION = 1

SUPPORTED_ADAPTERS = frozenset({"mock", "claude", "codex"})
GATE_STEPS = frozenset({"research", "draft", "review"})

_FRAMEWORK_MANAGED_KEYS = frozenset({"required", "human_override_reason"})


class TraceMismatch(Exception):
    """Raised when a trace is missing, malformed, or its digest does not match."""


def strict_mode_enabled() -> bool:
    return os.environ.get(STRICT_MODE_ENV, "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def extract_traceable_output(step_payload: dict) -> dict:
    """Return the slice of ``step_payload`` that is digested into a trace.

    Both the adapter (at trace creation) and the gate verifier call this
    to guarantee they agree on the exact dict being hashed.
    """
    return {
        key: value
        for key, value in step_payload.items()
        if not key.startswith("_") and key not in _FRAMEWORK_MANAGED_KEYS
    }


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _digest(payload: bytes) -> str:
    secret = os.environ.get(TRACE_SECRET_ENV, "").encode("utf-8")
    if secret:
        return "hmac-sha256:" + hmac.new(secret, payload, hashlib.sha256).hexdigest()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _output_digest_input(
    *,
    step: str,
    adapter: str,
    model: str,
    session_id: str | None,
    output: Any,
) -> dict:
    return {
        "schema_version": TRACE_SCHEMA_VERSION,
        "step": step,
        "adapter": adapter,
        "model": model,
        "session_id": session_id,
        "output": output,
    }


def compute_trace(
    *,
    step: str,
    adapter: str,
    model: str,
    session_id: str | None,
    prompt: str,
    output: Any,
    timestamp: str,
) -> dict:
    """Build a trace record for a sub-agent invocation.

    ``timestamp`` is passed in by the caller (not generated here) so traces
    are reproducible under retry / replay — the digest otherwise would not.
    """
    if adapter not in SUPPORTED_ADAPTERS:
        raise ValueError(f"unknown adapter: {adapter!r}")
    if not isinstance(step, str) or not step:
        raise ValueError("step must be a non-empty string")
    if not isinstance(model, str) or not model:
        raise ValueError("model must be a non-empty string")
    if not isinstance(prompt, str):
        raise ValueError("prompt must be a string")
    if not isinstance(timestamp, str) or not timestamp:
        raise ValueError("timestamp must be a non-empty ISO-8601 string")

    output_digest = _digest(
        _canonical_json(
            _output_digest_input(
                step=step,
                adapter=adapter,
                model=model,
                session_id=session_id,
                output=output,
            )
        )
    )
    prompt_digest = "sha256:" + hashlib.sha256(prompt.encode("utf-8")).hexdigest()

    return {
        "schema_version": TRACE_SCHEMA_VERSION,
        "step": step,
        "adapter": adapter,
        "model": model,
        "session_id": session_id,
        "timestamp": timestamp,
        "prompt_digest": prompt_digest,
        "output_digest": output_digest,
    }


def verify_trace(
    *,
    step: str,
    trace: dict | None,
    output: Any,
    human_override_reason: str | None = None,
) -> None:
    """Verify a trace's ``output_digest`` against the step's current output.

    Raises :class:`TraceMismatch` on any failure. A non-empty
    ``human_override_reason`` bypasses verification; callers are expected
    to record the override on the gate result.
    """
    if human_override_reason:
        if not isinstance(human_override_reason, str) or not human_override_reason.strip():
            raise TraceMismatch("human_override_reason must be a non-empty string")
        return

    if not isinstance(trace, dict):
        raise TraceMismatch(f"no sub_agent trace recorded for step {step!r}")

    if trace.get("schema_version") != TRACE_SCHEMA_VERSION:
        raise TraceMismatch(
            f"trace schema_version {trace.get('schema_version')!r} does not match "
            f"supported version {TRACE_SCHEMA_VERSION}"
        )

    if trace.get("step") != step:
        raise TraceMismatch(
            f"trace step {trace.get('step')!r} does not match gate step {step!r}"
        )

    adapter = trace.get("adapter")
    if adapter not in SUPPORTED_ADAPTERS:
        raise TraceMismatch(f"trace adapter {adapter!r} is not supported")

    expected = _digest(
        _canonical_json(
            _output_digest_input(
                step=step,
                adapter=adapter,
                model=trace.get("model", ""),
                session_id=trace.get("session_id"),
                output=output,
            )
        )
    )

    if not hmac.compare_digest(expected, trace.get("output_digest", "")):
        raise TraceMismatch(
            f"trace output_digest for step {step!r} does not match current payload"
        )
