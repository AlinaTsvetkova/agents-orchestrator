import os
import sys
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orchestrator.integrity import (  # noqa: E402
    TRACE_SECRET_ENV,
    TraceMismatch,
    compute_trace,
    extract_traceable_output,
    verify_trace,
)


BASE = dict(
    step="research",
    adapter="mock",
    model="mock-v1",
    session_id=None,
    prompt="PROMPT",
    output={"completed": True, "findings": ["a", "b"]},
    timestamp="2026-11-12T12:00:00Z",
)


class ComputeTests(unittest.TestCase):
    def test_deterministic(self):
        self.assertEqual(compute_trace(**BASE), compute_trace(**BASE))

    def test_output_key_order_irrelevant(self):
        a = compute_trace(**{**BASE, "output": {"completed": True, "findings": ["a"]}})
        b = compute_trace(**{**BASE, "output": {"findings": ["a"], "completed": True}})
        self.assertEqual(a["output_digest"], b["output_digest"])

    def test_different_output_changes_digest(self):
        a = compute_trace(**BASE)
        b = compute_trace(**{**BASE, "output": {"completed": True, "findings": ["c"]}})
        self.assertNotEqual(a["output_digest"], b["output_digest"])

    def test_prompt_only_touches_prompt_digest(self):
        a = compute_trace(**BASE)
        b = compute_trace(**{**BASE, "prompt": "OTHER"})
        self.assertEqual(a["output_digest"], b["output_digest"])
        self.assertNotEqual(a["prompt_digest"], b["prompt_digest"])

    def test_hmac_when_secret_set(self):
        with mock.patch.dict(os.environ, {TRACE_SECRET_ENV: "s"}):
            trace = compute_trace(**BASE)
        self.assertTrue(trace["output_digest"].startswith("hmac-sha256:"))

    def test_plain_sha256_when_no_secret(self):
        os.environ.pop(TRACE_SECRET_ENV, None)
        trace = compute_trace(**BASE)
        self.assertTrue(trace["output_digest"].startswith("sha256:"))

    def test_rejects_unknown_adapter(self):
        with self.assertRaises(ValueError):
            compute_trace(**{**BASE, "adapter": "gemini"})

    def test_rejects_empty_model(self):
        with self.assertRaises(ValueError):
            compute_trace(**{**BASE, "model": ""})


class VerifyTests(unittest.TestCase):
    def test_valid_trace_passes(self):
        trace = compute_trace(**BASE)
        verify_trace(step="research", trace=trace, output=BASE["output"])

    def test_missing_trace_raises(self):
        with self.assertRaises(TraceMismatch):
            verify_trace(step="research", trace=None, output={})

    def test_tampered_output_detected(self):
        trace = compute_trace(**BASE)
        with self.assertRaises(TraceMismatch):
            verify_trace(
                step="research",
                trace=trace,
                output={"completed": True, "findings": ["forged"]},
            )

    def test_step_mismatch_detected(self):
        trace = compute_trace(**BASE)
        with self.assertRaises(TraceMismatch):
            verify_trace(step="draft", trace=trace, output=BASE["output"])

    def test_wrong_secret_fails(self):
        with mock.patch.dict(os.environ, {TRACE_SECRET_ENV: "right"}):
            trace = compute_trace(**BASE)
        with mock.patch.dict(os.environ, {TRACE_SECRET_ENV: "wrong"}):
            with self.assertRaises(TraceMismatch):
                verify_trace(step="research", trace=trace, output=BASE["output"])

    def test_human_override_bypasses(self):
        verify_trace(
            step="research",
            trace=None,
            output={},
            human_override_reason="manual verification by operator",
        )

    def test_blank_override_still_fails(self):
        with self.assertRaises(TraceMismatch):
            verify_trace(step="research", trace=None, output={}, human_override_reason="   ")


class ExtractTests(unittest.TestCase):
    def test_excludes_underscore_and_managed_keys(self):
        result = extract_traceable_output(
            {
                "completed": True,
                "findings": ["x"],
                "_sub_agent_trace": {"anything": "here"},
                "required": True,
                "human_override_reason": "hi",
            }
        )
        self.assertEqual(result, {"completed": True, "findings": ["x"]})


if __name__ == "__main__":
    unittest.main()
