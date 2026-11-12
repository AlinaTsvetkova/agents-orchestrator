"""CLI entry point: ``orchestrator run | resume | inspect``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from orchestrator.adapters import build_adapter, run_with_trace
from orchestrator.state_machine import (
    STEP_SEQUENCE,
    TRACED_STEPS,
    OverrideError,
    RunState,
    advance,
    apply_human_override,
    load_state,
    mark_failed,
    record_human_plan_approval,
    record_sub_agent_output,
    save_state,
)


def _default_runs_dir() -> Path:
    return Path("runs").resolve()


def _drive(state: RunState, runs_dir: Path, *, auto_approve_plan: bool, agent) -> None:
    """Run the state machine until it either completes or needs a human.

    Sub-agent gates are satisfied automatically using ``agent``. The
    human plan-approval gate is satisfied with ``auto_approve_plan`` when
    requested (demo mode); otherwise the run stops and asks the operator.
    """
    last_announced_step: str | None = None
    while True:
        step_before_advance = state.current_step
        handoff = advance(state)
        save_state(state, runs_dir)
        status = handoff["workflow_status"]

        if status == "completed":
            print(f"[done] run {state.run_id} completed")
            return

        if status == "running":
            next_step = (handoff["next_action"] or {}).get("step")
            if next_step and next_step != last_announced_step:
                print(f"[pass] {step_before_advance} -> advancing to {next_step}")
                last_announced_step = next_step
            # Loop to exercise the next step's gate.
            continue

        next_action = handoff["next_action"] or {}
        step = next_action.get("step")

        if status == "agent_action_required" and next_action.get("type") == "invoke_sub_agent":
            if step not in TRACED_STEPS:
                print(f"unexpected sub-agent request at step {step}", file=sys.stderr)
                return
            print(f"[run ] invoking {agent.adapter_name} for step {step}")
            try:
                output = run_with_trace(agent, step, state.task)
            except Exception as error:  # noqa: BLE001 — adapter errors become run-level failures
                mark_failed(state, step, error)
                save_state(state, runs_dir)
                print(
                    f"[fail] {step}: {type(error).__name__}: {error}",
                    file=sys.stderr,
                )
                print(
                    f"       resume with: orchestrator resume {state.run_id}",
                    file=sys.stderr,
                )
                return
            record_sub_agent_output(state, step, output)
            continue

        if status == "failed":
            last = (next_action or {}).get("last_error") or {}
            print(
                f"[fail] run {state.run_id} is in a failed state at '{step}': "
                f"{last.get('type', '?')}: {last.get('message', '?')}",
                file=sys.stderr,
            )
            print(
                f"       resume with: orchestrator resume {state.run_id}",
                file=sys.stderr,
            )
            return

        if status == "human_action_required" and step == "plan":
            if auto_approve_plan:
                print("[auto] auto-approving plan gate")
                record_human_plan_approval(state, notes=["auto-approved by --auto-plan"])
                continue
            print(
                f"[wait] run {state.run_id} paused at 'plan'. "
                f"Approve and resume with: orchestrator resume --approve-plan {state.run_id}"
            )
            return

        print(
            f"[warn] run {state.run_id} paused: workflow_status={status} step={step}",
            file=sys.stderr,
        )
        return


def cmd_run(args: argparse.Namespace) -> int:
    task = {"summary": args.task}
    state = RunState.new(task)
    state.current_step = STEP_SEQUENCE[0]
    runs_dir = Path(args.runs_dir) if args.runs_dir else _default_runs_dir()
    agent = build_adapter(args.adapter)
    _drive(state, runs_dir, auto_approve_plan=args.auto_plan, agent=agent)
    print(state.run_id)
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    runs_dir = Path(args.runs_dir) if args.runs_dir else _default_runs_dir()
    state = load_state(args.run_id, runs_dir)
    if args.approve_plan:
        record_human_plan_approval(state, notes=["approved via orchestrator resume"])
    agent = build_adapter(args.adapter)
    _drive(state, runs_dir, auto_approve_plan=False, agent=agent)
    return 0


def cmd_override(args: argparse.Namespace) -> int:
    runs_dir = Path(args.runs_dir) if args.runs_dir else _default_runs_dir()
    state = load_state(args.run_id, runs_dir)
    try:
        record = apply_human_override(
            state,
            step=args.step,
            reason=args.reason,
            operator=args.operator,
        )
    except OverrideError as error:
        print(f"override rejected: {error}", file=sys.stderr)
        return 1
    save_state(state, runs_dir)
    print(
        f"override applied: run={state.run_id} step={record['step']} "
        f"operator={record['operator'] or '<unset>'} at {record['applied_at']}"
    )
    return 0


def _format_summary(state: RunState) -> str:
    """Human-readable one-screen summary of a run."""
    completed_steps = [
        step
        for step in STEP_SEQUENCE
        if isinstance(state.results.get(step), dict)
        and state.results[step].get("status") == "completed"
    ]
    traces = state.payload.get("sub_agent_traces", {}) or {}
    overrides = state.payload.get("overrides", []) or []
    warnings = state.payload.get("sub_agent_trace_warnings", {}) or {}

    lines = [
        f"Run:       {state.run_id}",
        f"Task:      {state.task.get('summary', '<unset>')}",
        f"Status:    {state.status}  (workflow_status={state.workflow_status})",
        f"Current:   {state.current_step or '<unset>'}",
        f"Progress:  {len(completed_steps)}/{len(STEP_SEQUENCE)} steps "
        f"completed ({', '.join(completed_steps) or '<none>'})",
        f"Traces:    {len(traces)} verified"
        + (f" ({', '.join(sorted(traces))})" if traces else ""),
        f"Overrides: {len(overrides)}"
        + (f" ({', '.join(r['step'] for r in overrides)})" if overrides else ""),
        f"Warnings:  {len(warnings)}"
        + (f" ({', '.join(sorted(warnings))})" if warnings else ""),
        f"Created:   {state.created_at}",
        f"Updated:   {state.updated_at}",
    ]
    if state.status == "failed" and state.last_error:
        lines.append(
            f"Error:     at '{state.failed_step}' — "
            f"{state.last_error.get('type')}: {state.last_error.get('message')}"
        )
    return "\n".join(lines)


def cmd_inspect(args: argparse.Namespace) -> int:
    runs_dir = Path(args.runs_dir) if args.runs_dir else _default_runs_dir()
    state = load_state(args.run_id, runs_dir)
    if args.json:
        json.dump(state.to_json(), sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        print(_format_summary(state))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="orchestrator")
    parser.add_argument("--runs-dir", help="where to read/write run-state files")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="start a new workflow run")
    run.add_argument("task", help="short summary of the task")
    run.add_argument(
        "--adapter",
        choices=("mock", "claude", "codex"),
        default="mock",
        help="sub-agent adapter to use (default: mock, offline deterministic)",
    )
    run.add_argument(
        "--auto-plan",
        action="store_true",
        help="auto-approve the plan gate (for demos and tests)",
    )
    run.set_defaults(func=cmd_run)

    resume = sub.add_parser("resume", help="resume a paused run")
    resume.add_argument("run_id")
    resume.add_argument(
        "--adapter",
        choices=("mock", "claude", "codex"),
        default="mock",
        help="sub-agent adapter to use for remaining steps",
    )
    resume.add_argument(
        "--approve-plan",
        action="store_true",
        help="approve the plan gate before resuming",
    )
    resume.set_defaults(func=cmd_resume)

    override = sub.add_parser(
        "override",
        help="mark a sub-agent step complete under human ownership (bypasses trace verify)",
    )
    override.add_argument("run_id")
    override.add_argument(
        "step",
        choices=sorted(TRACED_STEPS),
        help="which sub-agent gate to override",
    )
    override.add_argument(
        "--reason",
        required=True,
        help="non-empty explanation (≥ 12 non-whitespace chars) recorded to the audit list",
    )
    override.add_argument(
        "--operator",
        help="identifier of the person applying the override (optional, recorded verbatim)",
    )
    override.set_defaults(func=cmd_override)

    inspect = sub.add_parser("inspect", help="print a summary of a run's current state")
    inspect.add_argument("run_id")
    inspect.add_argument(
        "--json",
        action="store_true",
        help="emit the full run-state JSON instead of the summary",
    )
    inspect.set_defaults(func=cmd_inspect)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
