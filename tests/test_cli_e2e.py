"""End-to-end CLI run against the mock agent, including schema validation of state."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import jsonschema  # noqa: E402

from orchestrator.cli import main  # noqa: E402
from orchestrator.state_machine import load_state  # noqa: E402


SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "run-state.schema.json"


class CliE2ETests(unittest.TestCase):
    def setUp(self):
        os.environ.pop("ORCHESTRATOR_STRICT_INTEGRITY", None)
        self._tmp = tempfile.TemporaryDirectory()
        self.runs = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_run_with_auto_plan_completes(self):
        rc = main(["--runs-dir", str(self.runs), "run", "--auto-plan", "write TL;DR on HMAC"])
        self.assertEqual(rc, 0)

        run_files = list(self.runs.glob("*.json"))
        self.assertEqual(len(run_files), 1)

        state_data = json.loads(run_files[0].read_text(encoding="utf-8"))
        self.assertEqual(state_data["status"], "completed")

        # Every sub-agent gate must have produced a trace.
        traces = state_data["payload"]["sub_agent_traces"]
        self.assertEqual(set(traces), {"research", "draft", "review"})

        # Validate the state JSON against the schema we ship.
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        jsonschema.validate(state_data, schema)

    def test_run_without_auto_plan_pauses_and_resumes(self):
        rc = main(["--runs-dir", str(self.runs), "run", "study resumable workflows"])
        self.assertEqual(rc, 0)
        run_files = list(self.runs.glob("*.json"))
        state = load_state(run_files[0].stem, self.runs)
        self.assertEqual(state.status, "paused")
        self.assertEqual(state.workflow_status, "human_action_required")

        rc2 = main(["--runs-dir", str(self.runs), "resume", "--approve-plan", state.run_id])
        self.assertEqual(rc2, 0)
        state_after = load_state(state.run_id, self.runs)
        self.assertEqual(state_after.status, "completed")


class CliInspectSummaryTests(unittest.TestCase):
    def setUp(self):
        os.environ.pop("ORCHESTRATOR_STRICT_INTEGRITY", None)
        self._tmp = tempfile.TemporaryDirectory()
        self.runs = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_summary_after_completed_run(self):
        main(["--runs-dir", str(self.runs), "run", "--auto-plan", "demo"])
        state_path = next(self.runs.glob("*.json"))
        run_id = state_path.stem

        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(["--runs-dir", str(self.runs), "inspect", run_id])
        self.assertEqual(rc, 0)
        summary = buf.getvalue()
        self.assertIn("Status:    completed", summary)
        self.assertIn("5/5 steps completed", summary)
        self.assertIn("3 verified", summary)
        self.assertIn("plan, research, draft, review, finalize", summary)

    def test_json_mode_still_available(self):
        main(["--runs-dir", str(self.runs), "run", "--auto-plan", "demo"])
        state_path = next(self.runs.glob("*.json"))
        run_id = state_path.stem

        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(["--runs-dir", str(self.runs), "inspect", run_id, "--json"])
        self.assertEqual(rc, 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["run_id"], run_id)
        self.assertEqual(data["status"], "completed")


if __name__ == "__main__":
    unittest.main()
