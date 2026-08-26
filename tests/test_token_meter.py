"""Unit tests for TokenMeter (no network)."""

from __future__ import annotations

import unittest

from token_meter import TokenMeter, install_litellm_callback, meter


class TokenMeterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.local = TokenMeter()

    def test_record_and_totals(self) -> None:
        self.local.record(
            model="vertex_ai/gemini-2.5-pro",
            prompt_tokens=100,
            completion_tokens=40,
            had_usage=True,
        )
        self.local.record(
            model="vertex_ai/gemini-2.5-flash",
            prompt_tokens=50,
            completion_tokens=10,
            had_usage=True,
        )
        data = self.local.as_dict()
        self.assertEqual(data["calls"], 2)
        self.assertEqual(data["prompt_tokens"], 150)
        self.assertEqual(data["completion_tokens"], 50)
        self.assertEqual(data["total_tokens"], 200)
        self.assertEqual(data["by_model"]["vertex_ai/gemini-2.5-pro"]["total_tokens"], 140)
        self.assertEqual(data["by_model"]["vertex_ai/gemini-2.5-flash"]["total_tokens"], 60)

    def test_reset_clears(self) -> None:
        self.local.record(
            model="m",
            prompt_tokens=1,
            completion_tokens=1,
            had_usage=True,
        )
        self.local.reset()
        self.assertEqual(self.local.as_dict()["total_tokens"], 0)
        self.assertEqual(self.local.calls, 0)

    def test_no_usage_flag(self) -> None:
        self.local.record(
            model="m",
            prompt_tokens=0,
            completion_tokens=0,
            had_usage=False,
        )
        self.assertEqual(self.local.calls_without_usage, 1)
        self.assertIn("no_usage=1", self.local.format_one_line())

    def test_format_markdown(self) -> None:
        self.local.record(
            model="vertex_ai/gemini-2.5-pro",
            prompt_tokens=10,
            completion_tokens=5,
            had_usage=True,
        )
        md = self.local.format_markdown()
        self.assertIn("## Uso de tokens (LLM)", md)
        self.assertIn("Total: 15", md)
        self.assertIn("`vertex_ai/gemini-2.5-pro`", md)

    def test_snapshot_is_copy(self) -> None:
        self.local.record(
            model="m",
            prompt_tokens=3,
            completion_tokens=2,
            had_usage=True,
        )
        snap = self.local.snapshot()
        self.local.record(
            model="m",
            prompt_tokens=1,
            completion_tokens=1,
            had_usage=True,
        )
        self.assertEqual(snap["total_tokens"], 5)
        self.assertEqual(self.local.total_tokens, 7)

    def test_install_callback_idempotent(self) -> None:
        install_litellm_callback()
        install_litellm_callback()
        self.assertIs(meter, meter)


if __name__ == "__main__":
    unittest.main()
