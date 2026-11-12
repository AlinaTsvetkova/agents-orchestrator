import io
import json
import logging
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orchestrator.structured_log import (  # noqa: E402
    LOG_FORMAT_ENV,
    LOG_LEVEL_ENV,
    get_logger,
    reset_for_tests,
)


class StructuredLogTests(unittest.TestCase):
    def setUp(self):
        reset_for_tests()
        self.buffer = io.StringIO()

    def tearDown(self):
        reset_for_tests()

    def test_json_format_promotes_extras(self):
        with mock.patch.dict(os.environ, {LOG_FORMAT_ENV: "json"}, clear=False):
            log = get_logger("x")
            logging.getLogger("orchestrator").handlers[0].stream = self.buffer
            log.info("handoff_decision", extra={"run_id": "r-1", "step": "research"})

        record = json.loads(self.buffer.getvalue().strip())
        self.assertEqual(record["event"], "handoff_decision")
        self.assertEqual(record["run_id"], "r-1")
        self.assertEqual(record["step"], "research")

    def test_level_env_filters(self):
        with mock.patch.dict(os.environ, {LOG_LEVEL_ENV: "WARNING", LOG_FORMAT_ENV: "json"}, clear=False):
            log = get_logger("x")
            logging.getLogger("orchestrator").handlers[0].stream = self.buffer
            log.info("filtered")
            log.warning("kept")
        out = self.buffer.getvalue()
        self.assertNotIn("filtered", out)
        self.assertIn("kept", out)


if __name__ == "__main__":
    unittest.main()
