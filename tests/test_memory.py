"""Unit tests for the on-disk artifact store (sprint 6.3)."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

import memory
from contracts import (
    AgentKind,
    Complexity,
    Confidence,
    DomainSkip,
    Finding,
    ResearchPlan,
    SourceQuality,
    SourceRef,
    SubTask,
    SubTaskResult,
)


def _plan() -> ResearchPlan:
    return ResearchPlan(
        original_query="q",
        interpretation="i",
        scope="s",
        as_of=None,
        complexity=Complexity.lookup,
        angles_considered=["a"],
        domains_skipped=[DomainSkip(kind=AgentKind.SOCIAL, reason="not registered")],
        subtasks=[
            SubTask(
                id="t1",
                title="Find X",
                objective="Find X",
                output_format="SubTaskResult",
                sibling_boundaries="",
                success_criteria="X is found",
                max_calls=4,
            )
        ],
    )


def _result(subtask_id: str = "t1", n_findings: int = 1, n_gaps: int = 0) -> SubTaskResult:
    findings = [
        Finding(
            claim=f"Claim {i}",
            sources=[
                SourceRef(
                    domain=AgentKind.WEB,
                    locator=f"https://example.org/{i}",
                    title=f"Source {i}",
                    quality=SourceQuality.secondary,
                )
            ],
            confidence=Confidence.medium,
        )
        for i in range(n_findings)
    ]
    gaps = [f"Gap {i}" for i in range(n_gaps)]
    return SubTaskResult(subtask_id=subtask_id, findings=findings, gaps=gaps, calls_used=2)


@dataclass
class _FakeToolCall:
    name: str
    arguments: Any


@dataclass
class _FakeStep:
    tool_calls: list[_FakeToolCall] | None
    observations: str | None


class MemoryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._patcher = patch.object(memory, "MEMORY_ROOT", Path(self._tmp.name) / "memory")
        self._patcher.start()

    def tearDown(self) -> None:
        self._patcher.stop()
        self._tmp.cleanup()

    def test_save_plan_writes_json_and_returns_path(self) -> None:
        path = memory.save_plan("run-1", _plan())
        self.assertTrue(path.exists())
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["original_query"], "q")
        self.assertEqual(path.parent, memory.MEMORY_ROOT / "run-1")

    def test_run_id_is_sanitized_for_the_filesystem(self) -> None:
        path = memory.save_plan("2026-09-03T12:00:00Z/../weird", _plan())
        self.assertTrue(str(path).startswith(str(memory.MEMORY_ROOT)))
        self.assertNotIn("..", path.parts)

    def test_save_subtask_result_returns_artifact_ref_with_real_path(self) -> None:
        result = _result(n_findings=3, n_gaps=1)
        artifact = memory.save_subtask_result("run-1", result)
        self.assertEqual(artifact.subtask_id, "t1")
        self.assertEqual(artifact.type, "SubTaskResult")
        self.assertIsNotNone(artifact.path)
        on_disk = memory.MEMORY_ROOT.parent / artifact.path
        self.assertTrue(on_disk.exists())
        payload = json.loads(on_disk.read_text(encoding="utf-8"))
        self.assertEqual(len(payload["findings"]), 3)
        # The persisted copy is the plain worker output — no self-referential
        # artifact baked into what's on disk.
        self.assertEqual(payload.get("artifacts", []), [])

    def test_summary_is_at_most_three_lines_and_deterministic(self) -> None:
        result = _result(n_findings=5, n_gaps=2)
        first = memory.save_subtask_result("run-1", result).summary
        second = memory.save_subtask_result("run-2", result).summary
        self.assertEqual(first, second)
        self.assertLessEqual(len(first.splitlines()), 3)
        self.assertIn("5 achado", first)

    def test_summary_handles_no_findings(self) -> None:
        result = _result(n_findings=0, n_gaps=1)
        summary = memory.save_subtask_result("run-1", result).summary
        self.assertIn("Nenhum achado", summary)

    def test_extract_documents_keeps_only_document_tools_with_observations(self) -> None:
        steps = [
            _FakeStep(
                tool_calls=[_FakeToolCall(name="firecrawl_scrape", arguments={"url": "https://x.org/a"})],
                observations="page text",
            ),
            _FakeStep(
                tool_calls=[_FakeToolCall(name="firecrawl_search", arguments={"query": "x"})],
                observations="search results",
            ),
            _FakeStep(
                tool_calls=[_FakeToolCall(name="final_answer", arguments={"answer": "{}"})],
                observations=None,
            ),
            _FakeStep(tool_calls=None, observations=None),
        ]
        documents = memory.extract_documents(steps)
        self.assertEqual(len(documents), 2)
        scrape_doc = next(d for d in documents if d["tool"] == "firecrawl_scrape")
        self.assertEqual(scrape_doc["locator"], "https://x.org/a")
        self.assertEqual(scrape_doc["content"], "page text")
        search_doc = next(d for d in documents if d["tool"] == "firecrawl_search")
        self.assertIsNone(search_doc["locator"])  # no "url" key in search arguments

    def test_extract_documents_is_best_effort_on_malformed_steps(self) -> None:
        class Weird:
            pass

        documents = memory.extract_documents([Weird(), None, 42])
        self.assertEqual(documents, [])

    def test_save_documents_returns_none_when_empty(self) -> None:
        self.assertIsNone(memory.save_documents("run-1", "t1", []))

    def test_save_documents_writes_file_when_present(self) -> None:
        docs = [{"tool": "firecrawl_scrape", "locator": "https://x.org", "content": "hi", "arguments": {}}]
        path = memory.save_documents("run-1", "t1", docs)
        self.assertIsNotNone(path)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), docs)


if __name__ == "__main__":
    unittest.main()
