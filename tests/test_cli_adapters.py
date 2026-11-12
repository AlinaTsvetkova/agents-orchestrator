"""Unit tests for the real CLI adapters (claude, codex).

Subprocess is mocked — no binary is actually invoked.
"""

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orchestrator.adapters import (  # noqa: E402
    CLIAdapterError,
    ClaudeCLIAdapter,
    CodexCLIAdapter,
    build_adapter,
    build_prompt,
    MockAgent,
)


TASK = {"summary": "demo task"}


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    """Return a fake subprocess.CompletedProcess."""
    return subprocess.CompletedProcess(args=["fake"], returncode=returncode, stdout=stdout, stderr=stderr)


class PromptBuilderTests(unittest.TestCase):
    def test_each_step_has_a_contract(self):
        for step in ("research", "draft", "review"):
            prompt = build_prompt(step, TASK)
            self.assertIn("demo task", prompt)
            self.assertIn("JSON object", prompt)
            self.assertIn(step, prompt)

    def test_unknown_step_rejected(self):
        with self.assertRaises(ValueError):
            build_prompt("plan", TASK)


class ClaudeCLIAdapterTests(unittest.TestCase):
    def _envelope(self, body: dict) -> str:
        return json.dumps({"type": "result", "result": json.dumps(body), "is_error": False})

    def test_parses_single_envelope(self):
        agent = ClaudeCLIAdapter(binary="claude")
        body = {"completed": True, "findings": ["a"], "sources": ["s://1"]}
        with mock.patch("subprocess.run", return_value=_completed(stdout=self._envelope(body))):
            output, prompt = agent.run("research", TASK)
        self.assertEqual(output, body)
        self.assertIn("research", prompt)

    def test_parses_event_stream(self):
        agent = ClaudeCLIAdapter()
        body = {"completed": True, "body": "x", "word_count": 1}
        stream = [
            {"type": "system", "subtype": "init"},
            {"type": "assistant", "content": "thinking..."},
            {"type": "result", "result": json.dumps(body), "is_error": False},
        ]
        with mock.patch("subprocess.run", return_value=_completed(stdout=json.dumps(stream))):
            output, _ = agent.run("draft", TASK)
        self.assertEqual(output, body)

    def test_is_error_envelope_raises(self):
        agent = ClaudeCLIAdapter()
        envelope = json.dumps({"type": "result", "result": "rate limited", "is_error": True})
        with mock.patch("subprocess.run", return_value=_completed(stdout=envelope)):
            with self.assertRaises(CLIAdapterError):
                agent.run("research", TASK)

    def test_non_zero_exit_raises(self):
        agent = ClaudeCLIAdapter()
        with mock.patch("subprocess.run", return_value=_completed(stderr="boom", returncode=1)):
            with self.assertRaises(CLIAdapterError) as ctx:
                agent.run("research", TASK)
        self.assertIn("boom", str(ctx.exception))

    def test_timeout_raises(self):
        agent = ClaudeCLIAdapter(timeout_seconds=1)
        with mock.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["claude"], timeout=1),
        ):
            with self.assertRaises(CLIAdapterError) as ctx:
                agent.run("research", TASK)
        self.assertIn("timed out", str(ctx.exception))

    def test_malformed_json_raises(self):
        agent = ClaudeCLIAdapter()
        with mock.patch("subprocess.run", return_value=_completed(stdout="not json")):
            with self.assertRaises(CLIAdapterError):
                agent.run("research", TASK)

    def test_command_includes_model_and_output_format(self):
        agent = ClaudeCLIAdapter(binary="/opt/claude", model="claude-sonnet-4-6")
        cmd = agent.build_command("research")
        self.assertEqual(cmd[0], "/opt/claude")
        self.assertIn("--model", cmd)
        self.assertIn("claude-sonnet-4-6", cmd)
        self.assertIn("--output-format", cmd)
        self.assertIn("json", cmd)


class CodexCLIAdapterTests(unittest.TestCase):
    def test_parses_single_envelope(self):
        agent = CodexCLIAdapter()
        body = {"completed": True, "findings": ["a"], "sources": []}
        envelope = json.dumps({"last_message": body})
        with mock.patch("subprocess.run", return_value=_completed(stdout=envelope)):
            output, _ = agent.run("research", TASK)
        self.assertEqual(output, body)

    def test_parses_ndjson_stream_picks_last_result(self):
        agent = CodexCLIAdapter()
        body = {"completed": True, "approved": True, "comments": []}
        events = [
            json.dumps({"type": "token", "text": "..."}),
            json.dumps({"type": "progress"}),
            json.dumps({"last_message": body}),
        ]
        with mock.patch("subprocess.run", return_value=_completed(stdout="\n".join(events))):
            output, _ = agent.run("review", TASK)
        self.assertEqual(output, body)

    def test_result_as_json_string_is_decoded(self):
        agent = CodexCLIAdapter()
        body = {"completed": True, "body": "...", "word_count": 2}
        envelope = json.dumps({"result": json.dumps(body)})
        with mock.patch("subprocess.run", return_value=_completed(stdout=envelope)):
            output, _ = agent.run("draft", TASK)
        self.assertEqual(output, body)

    def test_no_result_event_raises(self):
        agent = CodexCLIAdapter()
        events = [json.dumps({"type": "token"})]
        with mock.patch("subprocess.run", return_value=_completed(stdout="\n".join(events))):
            with self.assertRaises(CLIAdapterError):
                agent.run("research", TASK)


class BuildAdapterTests(unittest.TestCase):
    def test_mock(self):
        self.assertIsInstance(build_adapter("mock"), MockAgent)

    def test_claude(self):
        self.assertIsInstance(build_adapter("claude"), ClaudeCLIAdapter)

    def test_codex(self):
        self.assertIsInstance(build_adapter("codex"), CodexCLIAdapter)

    def test_unknown(self):
        with self.assertRaises(ValueError):
            build_adapter("gemini")


if __name__ == "__main__":
    unittest.main()
