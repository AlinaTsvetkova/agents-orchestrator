# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — 2026-11-12

First tagged release. The orchestrator runs end-to-end against a mock
sub-agent and against the Anthropic Claude Code and OpenAI Codex CLIs,
with cryptographic sub-agent output verification at every traced gate.

### Added

- **State machine.** Five-step workflow (`plan` → `research` → `draft`
  → `review` → `finalize`) with one human gate, three sub-agent gates,
  and one deterministic composition step. Resumable from persisted JSON
  state.
- **Integrity layer.** `compute_trace` / `verify_trace` in
  `orchestrator.integrity` with canonical-JSON digests, HMAC-SHA256 when
  `ORCHESTRATOR_TRACE_SECRET` is set (plain SHA-256 fallback),
  timing-safe comparison, strict and soft verification modes, and a
  `human_override_reason` bypass.
- **Sub-agent adapters.** `MockAgent` (offline, deterministic),
  `ClaudeCLIAdapter` (`claude -p --output-format json`, soft schema
  enforcement via prompt), `CodexCLIAdapter` (`codex exec --json
  --output-schema <file>`, hard enforcement via JSON schema tempfile).
- **CLI.** `orchestrator run`, `resume`, `inspect`, `override`, with
  `--adapter mock|claude|codex` selection and a human-readable summary
  view (`--json` for the raw state).
- **Human-override audit trail.** `apply_human_override` records both
  on the step payload and an append-only `payload.overrides` list.
  Reason strings shorter than 12 non-whitespace chars are rejected.
- **Failed-state transitions.** Gate or adapter exceptions set
  `state.status = "failed"`, record a structured `last_error` envelope,
  and are cleared automatically on resume.
- **Load-time schema validation.** `load_state` validates against
  `schemas/run-state.schema.json` and raises `StateFileCorrupted` on
  malformed, incomplete, or tampered state files.
- **Structured logging.** JSON records to stderr keyed by `run_id`
  across handoff decisions, integrity checks, state saves, agent
  invocations, and overrides. Configurable via
  `ORCHESTRATOR_LOG_LEVEL` and `ORCHESTRATOR_LOG_FORMAT`.
- **Documentation.** README with threat model, Mermaid state-machine
  and trust-boundary diagrams, real-agent setup, override and inspect
  examples; `docs/DESIGN.md` with rationale for every non-obvious
  design decision.
- **CI.** GitHub Actions matrix across Python 3.11, 3.12, 3.13.

### Tested

- 69 unit, integration, and end-to-end tests covering integrity
  primitives, state-machine flow, soft-mode warnings, CLI runs with
  mock and failing adapters, CLI adapter envelope parsing (success,
  error, timeout, malformed), human overrides, schema-corruption
  detection, and JSON-log emission.
