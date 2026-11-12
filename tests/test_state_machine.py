import os
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orchestrator.adapters import MockAgent, run_with_trace  # noqa: E402
from orchestrator.integrity import compute_trace, extract_traceable_output  # noqa: E402
from orchestrator.state_machine import (  # noqa: E402
    RunState,
    advance,
    load_state,
    record_human_plan_approval,
    record_sub_agent_output,
    save_state,
)


class HandoffFlowTests(unittest.TestCase):
    def setUp(self):
        os.environ.pop("ORCHESTRATOR_STRICT_INTEGRITY", None)
        self._tmp = tempfile.TemporaryDirectory()
        self.runs = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_plan_gate_requires_human(self):
        state = RunState.new({"summary": "demo"})
        state.current_step = "plan"
        handoff = advance(state)
        self.assertEqual(handoff["workflow_status"], "human_action_required")
        self.assertEqual(handoff["next_action"]["step"], "plan")

    def test_plan_approval_advances_to_research(self):
        state = RunState.new({"summary": "demo"})
        state.current_step = "plan"
        record_human_plan_approval(state)
        handoff = advance(state)
        self.assertEqual(handoff["workflow_status"], "running")
        self.assertEqual(handoff["next_action"]["step"], "research")

    def test_research_gate_without_output_asks_for_agent(self):
        state = RunState.new({"summary": "demo"})
        state.current_step = "research"
        handoff = advance(state)
        self.assertEqual(handoff["workflow_status"], "agent_action_required")
        self.assertEqual(handoff["next_action"]["step"], "research")

    def test_research_gate_with_valid_trace_passes(self):
        state = RunState.new({"summary": "demo"})
        state.current_step = "research"
        agent = MockAgent()
        output = run_with_trace(agent, "research", state.task)
        record_sub_agent_output(state, "research", output)

        handoff = advance(state)
        self.assertEqual(handoff["workflow_status"], "running")
        self.assertEqual(handoff["next_action"]["step"], "draft")

    def test_tampered_output_blocks_gate(self):
        state = RunState.new({"summary": "demo"})
        state.current_step = "research"
        agent = MockAgent()
        output = run_with_trace(agent, "research", state.task)
        record_sub_agent_output(state, "research", output)
        # Lead agent tampering with the step payload after the fact.
        state.payload["research"]["findings"] = ["forged"]

        handoff = advance(state)
        self.assertEqual(handoff["workflow_status"], "agent_action_required")
        self.assertEqual(handoff["next_action"]["reason"], "integrity_check_failed")

    def test_human_override_bypasses_trace(self):
        state = RunState.new({"summary": "demo"})
        state.current_step = "research"
        state.payload["research"] = {
            "completed": True,
            "findings": ["manual"],
            "human_override_reason": "operator performed research manually",
        }
        handoff = advance(state)
        self.assertEqual(handoff["workflow_status"], "running")

    def test_full_run_completes_end_to_end(self):
        state = RunState.new({"summary": "demo"})
        state.current_step = "plan"
        record_human_plan_approval(state)

        agent = MockAgent()
        for _ in range(20):
            handoff = advance(state)
            status = handoff["workflow_status"]
            if status == "completed":
                break
            if status == "running":
                continue
            next_action = handoff["next_action"]
            if next_action["type"] == "invoke_sub_agent":
                step = next_action["step"]
                output = run_with_trace(agent, step, state.task)
                record_sub_agent_output(state, step, output)
            else:
                self.fail(f"unexpected pause: {handoff}")
        self.assertEqual(state.status, "completed")
        self.assertIn("artifact", state.payload["finalize"])

    def test_save_and_load_roundtrip(self):
        state = RunState.new({"summary": "demo"})
        state.current_step = "plan"
        record_human_plan_approval(state)
        advance(state)
        save_state(state, self.runs)

        loaded = load_state(state.run_id, self.runs)
        self.assertEqual(loaded.run_id, state.run_id)
        self.assertEqual(loaded.current_step, "research")
        self.assertTrue(loaded.payload["plan"]["approved"])


class StateMachineIntegrityModeTests(unittest.TestCase):
    def setUp(self):
        self._prior = os.environ.pop("ORCHESTRATOR_STRICT_INTEGRITY", None)

    def tearDown(self):
        if self._prior is not None:
            os.environ["ORCHESTRATOR_STRICT_INTEGRITY"] = self._prior

    def test_soft_mode_records_warning_instead_of_blocking(self):
        os.environ["ORCHESTRATOR_STRICT_INTEGRITY"] = "0"
        state = RunState.new({"summary": "demo"})
        state.current_step = "research"
        # Simulate a pre-trace legacy payload: completed but no trace.
        state.payload["research"] = {"completed": True, "findings": ["legacy"]}

        handoff = advance(state)
        self.assertEqual(handoff["workflow_status"], "running")
        self.assertIn("research", state.payload["sub_agent_trace_warnings"])


if __name__ == "__main__":
    unittest.main()
