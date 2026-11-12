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
    RunState,
    advance,
    load_state,
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
    while True:
        handoff = advance(state)
        save_state(state, runs_dir)
        status = handoff["workflow_status"]

        if status == "completed":
            print(f"run {state.run_id} completed")
            return

        if status == "running":
            # Gate passed; advance() has moved current_step forward. Loop to
            # exercise the next step's gate.
            continue

        next_action = handoff["next_action"] or {}
        step = next_action.get("step")

        if status == "agent_action_required" and next_action.get("type") == "invoke_sub_agent":
            if step not in TRACED_STEPS:
                print(f"unexpected sub-agent request at step {step}", file=sys.stderr)
                return
            output = run_with_trace(agent, step, state.task)
            record_sub_agent_output(state, step, output)
            continue

        if status == "human_action_required" and step == "plan":
            if auto_approve_plan:
                record_human_plan_approval(state, notes=["auto-approved by --auto-plan"])
                continue
            print(
                f"run {state.run_id} is paused at step 'plan'. "
                f"Approve the plan and resume with: orchestrator resume {state.run_id}",
            )
            return

        print(
            f"run {state.run_id} paused: workflow_status={status} step={step}",
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


def cmd_inspect(args: argparse.Namespace) -> int:
    runs_dir = Path(args.runs_dir) if args.runs_dir else _default_runs_dir()
    state = load_state(args.run_id, runs_dir)
    json.dump(state.to_json(), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
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

    inspect = sub.add_parser("inspect", help="print a run's current state as JSON")
    inspect.add_argument("run_id")
    inspect.set_defaults(func=cmd_inspect)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
