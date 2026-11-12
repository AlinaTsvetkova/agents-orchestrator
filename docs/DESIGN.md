# Design: trust-boundary pattern for multi-agent orchestration

## Problem

A lead agent orchestrates one or more sub-agents by reading their outputs
from a shared run-state file. Nothing in a typical setup stops the lead
agent from writing a plausible-looking sub-agent output directly into the
state file and advancing the workflow as if the sub-agent had run.

If sub-agents are meant to be an *independent* verification layer (reviewer,
auditor, critic), that silent-forgery path breaks the entire trust model
every time the workflow passes through one of their gates.

## Threat model

- **Adversary:** the lead agent (or anything writing to its state file).
- **Capability:** full read/write to run-state JSON on disk.
- **Goal:** mark a sub-agent gate as complete with chosen contents, without
  running the sub-agent.
- **Out of scope:** attacker who can read the adapter's environment or
  inject code into the adapter process. Such an attacker already defeats
  sub-agent independence by definition.

## Design

Every sub-agent adapter produces an **invocation trace** committing to the
output it returned. The trace is persisted to the authoritative map
`payload.sub_agent_traces[<step>]`. At each sub-agent gate, the orchestrator
recomputes the digest from the current step payload and compares it to the
stored digest. A mismatch (or a missing trace) blocks the gate.

### Trace record

```json
{
  "schema_version": 1,
  "step": "research",
  "adapter": "mock",
  "model": "mock-v1",
  "session_id": null,
  "timestamp": "2026-11-12T12:00:00Z",
  "prompt_digest": "sha256:...",
  "output_digest": "hmac-sha256:..."
}
```

### Two digests, not one

- `output_digest` is **authoritative**. It covers
  `{schema_version, step, adapter, model, session_id, output}` and is
  HMAC-SHA256 when `ORCHESTRATOR_TRACE_SECRET` is set, plain SHA-256
  otherwise. This is what the gate verifies.
- `prompt_digest` is **informational**, plain SHA-256 of the prompt the
  adapter sent. The prompt itself is not stored (prompts are large and may
  contain sensitive context). Keeping just the digest lets an operator
  investigate prompt-swap tampering offline by recomputing the prompt.

The output verification cannot depend on the prompt at runtime, because the
gate has no copy of the prompt — the adapter discards it after sending.

### Keys excluded from the digest

`extract_traceable_output(step_payload)` filters a step payload down to what
the trace covers:

- keys starting with `_` (adapter-side side-channels, e.g. `_sub_agent_trace`)
- `required` and `human_override_reason` (framework-managed, not agent output)

Both the adapter and the verifier call this helper so they cannot drift.

### Verify before mutate

Some gates mutate their own payload legitimately after the sub-agent runs —
for example, re-classifying a result based on cross-step context. The gate
therefore verifies the trace **first**, before any such mutation can change
the digested fields.

### Human override

A `human_override_reason: str` on a step payload bypasses verification. It is
recorded, not silenced. This is intended for one-off manual ownership of a
gate (operator did the research manually), not for turning the trust layer
off at scale.

### Strict / soft mode

`ORCHESTRATOR_STRICT_INTEGRITY=0` downgrades mismatches to warnings written
to `payload.sub_agent_trace_warnings[<step>]`. This exists so legacy runs
without traces can be resumed during a migration. Production should keep
strict mode on.

## Why this is an argument, not a product

The implementation here is deliberately thin. The payoff is in the pattern,
not the code. A production orchestrator inherits the same three design
ingredients from this reference:

1. **Adapter commits to output via trace**, so the lead agent cannot author
   sub-agent output silently.
2. **Shared `extract_traceable_output` contract**, so adapter and verifier
   can never disagree on what was hashed.
3. **Verify before any framework-side mutation**, so the digest covers the
   exact dict the sub-agent produced.

The rest — timestamps for replay reproducibility, schema versioning, the
two-digest split, timing-safe comparison — falls out of these three.

## Non-goals

- Secret rotation. The HMAC key is static. Rotation would invalidate all
  in-flight traces; worth building when you have multi-tenant operators,
  not before.
- Protection against a compromised adapter environment. If the lead agent
  can read the trace secret, this layer buys you nothing. Put the secret
  where the orchestrator can read it and the lead agent cannot (CI
  secrets, Temporal worker env, etc.).
- Covering `plan` and `finalize`. They are not sub-agent gates — plan is a
  human decision, finalize is deterministic composition.
