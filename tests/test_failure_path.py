"""Failed-state transitions, resume-from-failure, and corrupted-state detection."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orchestrator.state_machine import (  # noqa: E402
    RunState,
    StateFileCorrupted,
    advance,
    load_state,
    mark_failed,
    record_human_plan_approval,
    save_state,
)


class MarkFailedTests(unittest.TestCase):
    def test_transitions_to_failed_with_structured_error(self):
        state = RunState.new({"summary": "demo"})
        state.current_step = "research"

        handoff = mark_failed(state, "research", RuntimeError("upstream API down"))

        self.assertEqual(state.status, "failed")
        self.assertEqual(state.workflow_status, "failed")
        self.assertEqual(state.failed_step, "research")
        self.assertEqual(state.last_error, {"type": "RuntimeError", "message": "upstream API down"})
        self.assertEqual(handoff["workflow_status"], "failed")
        self.assertEqual(handoff["next_action"]["type"], "restart_failed_step")


class AdvanceCatchesGateExceptionsTests(unittest.TestCase):
    def test_check_gate_exception_marks_run_failed(self):
        import orchestrator.state_machine as sm

        state = RunState.new({"summary": "demo"})
        state.current_step = "research"
        original_check_gate = sm.check_gate

        def boom(_state, _step):
            raise RuntimeError("synthetic gate failure")

        sm.check_gate = boom
        try:
            handoff = advance(state)
        finally:
            sm.check_gate = original_check_gate

        self.assertEqual(handoff["workflow_status"], "failed")
        self.assertEqual(state.status, "failed")
        self.assertEqual(state.failed_step, "research")


class ResumeFromFailureTests(unittest.TestCase):
    def test_resume_clears_failure_markers_before_retry(self):
        state = RunState.new({"summary": "demo"})
        state.current_step = "research"
        mark_failed(state, "research", RuntimeError("transient"))

        # Simulate the sub-agent being satisfied (via human override — the
        # simplest trace-free path) and then re-advancing.
        state.payload["research"] = {
            "completed": True,
            "findings": ["x"],
            "sources": [],
            "human_override_reason": "retried research by hand after outage recovered",
        }
        handoff = advance(state)

        self.assertEqual(handoff["workflow_status"], "running")
        self.assertIsNone(state.failed_step)
        self.assertIsNone(state.last_error)


class SchemaValidationOnLoadTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.runs = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, name: str, body: str) -> None:
        (self.runs / f"{name}.json").write_text(body, encoding="utf-8")

    def test_valid_state_loads(self):
        state = RunState.new({"summary": "demo"})
        state.current_step = "plan"
        record_human_plan_approval(state)
        save_state(state, self.runs)
        loaded = load_state(state.run_id, self.runs)
        self.assertEqual(loaded.run_id, state.run_id)

    def test_malformed_json_raises(self):
        self._write("bad-id", "{not json")
        with self.assertRaises(StateFileCorrupted) as ctx:
            load_state("bad-id", self.runs)
        self.assertIn("not valid JSON", str(ctx.exception))

    def test_missing_required_field_raises(self):
        incomplete = {
            "run_id": "r",
            "task": {},
            "payload": {},
            "results": {},
            # status, workflow_status, timestamps missing
        }
        self._write("r", json.dumps(incomplete))
        with self.assertRaises(StateFileCorrupted) as ctx:
            load_state("r", self.runs)
        self.assertIn("does not match", str(ctx.exception))

    def test_unknown_status_rejected_by_enum(self):
        state = RunState.new({"summary": "demo"})
        save_state(state, self.runs)
        raw = json.loads((self.runs / f"{state.run_id}.json").read_text())
        raw["status"] = "spongebob"
        (self.runs / f"{state.run_id}.json").write_text(json.dumps(raw))
        with self.assertRaises(StateFileCorrupted):
            load_state(state.run_id, self.runs)

    def test_last_error_shape_enforced(self):
        state = RunState.new({"summary": "demo"})
        save_state(state, self.runs)
        raw = json.loads((self.runs / f"{state.run_id}.json").read_text())
        raw["last_error"] = {"type": "RuntimeError"}  # missing "message"
        (self.runs / f"{state.run_id}.json").write_text(json.dumps(raw))
        with self.assertRaises(StateFileCorrupted):
            load_state(state.run_id, self.runs)


class CliFailurePathTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.runs = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_adapter_exception_marks_run_failed_and_persists(self):
        import orchestrator.adapters as adapters_module
        from orchestrator.cli import main

        original = adapters_module.MockAgent.run

        def boom(self, step, task):
            if step == "draft":
                raise RuntimeError("synthetic draft failure")
            return original(self, step, task)

        adapters_module.MockAgent.run = boom
        try:
            rc = main(["--runs-dir", str(self.runs), "run", "--auto-plan", "demo"])
        finally:
            adapters_module.MockAgent.run = original

        # CLI exits 0 (user-facing success — the run is persisted) but state is failed.
        self.assertEqual(rc, 0)
        state_files = list(self.runs.glob("*.json"))
        self.assertEqual(len(state_files), 1)
        state = load_state(state_files[0].stem, self.runs)
        self.assertEqual(state.status, "failed")
        self.assertEqual(state.failed_step, "draft")
        self.assertEqual(state.last_error["type"], "RuntimeError")
        self.assertIn("synthetic", state.last_error["message"])


if __name__ == "__main__":
    unittest.main()
