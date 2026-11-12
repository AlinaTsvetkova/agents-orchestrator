"""Human-override behaviour at the state-machine and CLI levels."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orchestrator.cli import main  # noqa: E402
from orchestrator.state_machine import (  # noqa: E402
    OverrideError,
    RunState,
    advance,
    apply_human_override,
    load_state,
    record_human_plan_approval,
    save_state,
)


class ApplyHumanOverrideTests(unittest.TestCase):
    def test_sets_completed_and_reason_and_audit_record(self):
        state = RunState.new({"summary": "demo"})
        state.current_step = "research"

        record = apply_human_override(
            state,
            step="research",
            reason="operator ran the research manually during degraded upstream",
            operator="alice",
        )

        self.assertTrue(state.payload["research"]["completed"])
        self.assertTrue(state.payload["research"]["human_override_reason"].startswith("operator"))
        self.assertEqual(record["step"], "research")
        self.assertEqual(record["operator"], "alice")
        self.assertEqual(state.payload["overrides"], [record])

    def test_unknown_step_rejected(self):
        state = RunState.new({"summary": "demo"})
        with self.assertRaises(OverrideError):
            apply_human_override(state, step="plan", reason="valid reason text here")

    def test_short_reason_rejected(self):
        state = RunState.new({"summary": "demo"})
        with self.assertRaises(OverrideError):
            apply_human_override(state, step="research", reason="too short")

    def test_already_completed_run_rejected(self):
        state = RunState.new({"summary": "demo"})
        state.status = "completed"
        with self.assertRaises(OverrideError):
            apply_human_override(state, step="research", reason="valid reason text here")

    def test_override_lets_gate_advance(self):
        state = RunState.new({"summary": "demo"})
        state.current_step = "research"
        apply_human_override(
            state,
            step="research",
            reason="operator finished this manually",
        )
        handoff = advance(state)
        self.assertEqual(handoff["workflow_status"], "running")

    def test_multiple_overrides_append_to_audit_list(self):
        state = RunState.new({"summary": "demo"})
        apply_human_override(state, step="research", reason="first override, long enough")
        apply_human_override(state, step="draft", reason="second override, long enough")
        self.assertEqual([r["step"] for r in state.payload["overrides"]], ["research", "draft"])


class OverrideCliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.runs = Path(self._tmp.name)
        # Seed a run paused at the research gate.
        state = RunState.new({"summary": "demo"})
        state.current_step = "plan"
        record_human_plan_approval(state)
        advance(state)  # advances to research
        save_state(state, self.runs)
        self.run_id = state.run_id

    def tearDown(self):
        self._tmp.cleanup()

    def test_override_command_succeeds_and_persists(self):
        rc = main(
            [
                "--runs-dir",
                str(self.runs),
                "override",
                self.run_id,
                "research",
                "--reason",
                "operator took manual ownership of the research step",
                "--operator",
                "alice",
            ]
        )
        self.assertEqual(rc, 0)

        state = load_state(self.run_id, self.runs)
        self.assertEqual(state.payload["overrides"][0]["operator"], "alice")

    def test_override_with_short_reason_fails(self):
        rc = main(
            [
                "--runs-dir",
                str(self.runs),
                "override",
                self.run_id,
                "research",
                "--reason",
                "too short",
            ]
        )
        self.assertEqual(rc, 1)

    def test_override_with_invalid_step_blocked_by_argparse(self):
        with self.assertRaises(SystemExit):
            main(
                [
                    "--runs-dir",
                    str(self.runs),
                    "override",
                    self.run_id,
                    "plan",
                    "--reason",
                    "trying to override plan step which is human gate",
                ]
            )


if __name__ == "__main__":
    unittest.main()
