"""Unit tests for Describe source mapping (mocked Firecrawl HTTP)."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from contracts import Clarification, Complexity, describe_budget_for
from sources import (
    DomainRegistry,
    WebDomain,
    build_recon_queries,
    profiles_for_planner,
)


def _search_payload(urls: list[str]) -> dict:
    return {
        "success": True,
        "data": {
            "web": [
                {
                    "url": u,
                    "title": f"Title {i}",
                    "description": f"Snippet {i}",
                }
                for i, u in enumerate(urls)
            ]
        },
    }


class DescribeBudgetTests(unittest.TestCase):
    def test_budgets_match_plan(self) -> None:
        self.assertEqual(describe_budget_for(Complexity.lookup).max_sources, 25)
        self.assertEqual(describe_budget_for(Complexity.comparison).max_queries, 4)
        self.assertEqual(describe_budget_for(Complexity.exploratory).max_sources, 80)
        self.assertEqual(describe_budget_for(Complexity.deep).max_sources, 120)


class ReconQueryTests(unittest.TestCase):
    def test_build_recon_queries_dedupes_and_caps(self) -> None:
        queries = build_recon_queries(
            "Andrew Carnegie steel philanthropy biography",
            angles=["Homestead strike", "Carnegie libraries", "steel industry"],
            max_queries=4,
        )
        self.assertLessEqual(len(queries), 4)
        self.assertEqual(len(queries), len(set(q.casefold() for q in queries)))
        self.assertTrue(all(len(q.split()) <= 6 for q in queries))


class WebDescribeTests(unittest.TestCase):
    def test_multi_query_map_dedupes_and_caps(self) -> None:
        domain = WebDomain(api_url="http://test.local")
        call_n = {"n": 0}

        def fake_post(path: str, body: dict) -> dict:
            self.assertEqual(path, "/v2/search")
            call_n["n"] += 1
            # Overlapping URLs across calls to exercise dedupe.
            base = call_n["n"]
            urls = [
                f"https://example.com/a{base}",
                f"https://example.com/shared",
                f"https://britannica.com/topic/{base}",
                f"https://en.wikipedia.org/wiki/X{base}",
            ]
            return _search_payload(urls)

        with patch.object(domain, "_post", side_effect=fake_post):
            profile = domain.describe(
                "Andrew Carnegie",
                angles=["steel", "philanthropy", "Homestead", "libraries"],
                complexity=Complexity.comparison,
            )

        budget = describe_budget_for(Complexity.comparison)
        self.assertLessEqual(profile.mapped_total, budget.max_sources)
        self.assertEqual(profile.mapped_total, len(profile.mapped_sources))
        locators = [c.locator for c in profile.mapped_sources]
        self.assertEqual(len(locators), len(set(u.rstrip("/").casefold() for u in locators)))
        self.assertGreater(profile.mapped_total, 1)
        self.assertIn("example.com", profile.dominant_hosts)
        self.assertTrue(profile.available)
        self.assertIn("Mapped", profile.summary)

    def test_empty_recon_still_returns_profile(self) -> None:
        domain = WebDomain(api_url="http://test.local")

        with patch.object(domain, "_post", return_value={"success": True, "data": {"web": []}}):
            profile = domain.describe("obscure topic xyz", complexity=Complexity.lookup)

        self.assertEqual(profile.mapped_total, 0)
        self.assertEqual(profile.mapped_sources, [])
        self.assertTrue(any("no results" in g.lower() for g in profile.known_gaps))

    def test_partial_failure_continues(self) -> None:
        domain = WebDomain(api_url="http://test.local")
        state = {"n": 0}

        def fake_post(path: str, body: dict) -> dict:
            state["n"] += 1
            if state["n"] == 1:
                raise ConnectionError("boom")
            return _search_payload(
                [
                    "https://en.wikipedia.org/wiki/Andrew_Carnegie",
                    "https://www.britannica.com/money/Andrew-Carnegie",
                ]
            )

        with patch.object(domain, "_post", side_effect=fake_post):
            profile = domain.describe(
                "Andrew Carnegie",
                angles=["steel empire"],
                complexity=Complexity.lookup,
            )

        self.assertGreaterEqual(profile.mapped_total, 1)
        self.assertTrue(any("failed" in g.lower() for g in profile.known_gaps))

    def test_as_of_and_scope_in_gaps(self) -> None:
        domain = WebDomain(api_url="http://test.local")
        with patch.object(domain, "_post", return_value=_search_payload(["https://a.test/x"])):
            profile = domain.describe(
                "topic",
                scope="US only",
                as_of="1901-01-01",
                complexity=Complexity.lookup,
            )
        joined = " ".join(profile.known_gaps)
        self.assertIn("1901-01-01", joined)
        self.assertIn("US only", joined)


class RegistryAndPlannerPayloadTests(unittest.TestCase):
    def test_describe_all_passes_angles_and_complexity(self) -> None:
        domain = WebDomain(api_url="http://test.local")
        with patch.object(
            domain,
            "_post",
            return_value=_search_payload(
                [f"https://host.example/p{i}" for i in range(10)]
            ),
        ):
            registry = DomainRegistry([domain])
            clarification = Clarification(
                original_query="Who was Andrew Carnegie?",
                interpretation="Biography and steel empire of Andrew Carnegie",
                scope="Facts and major sources only",
                as_of=None,
                complexity=Complexity.deep,
                angles_considered=["steel", "philanthropy", "Homestead"],
            )
            profiles = registry.describe_all(clarification)

        self.assertEqual(len(profiles), 1)
        self.assertGreater(profiles[0].mapped_total, 0)
        # deep allows up to 8 queries * 20, capped at 120; with one stub batch we get 10.
        self.assertEqual(profiles[0].mapped_total, 10)

    def test_profiles_for_planner_truncates(self) -> None:
        from contracts import AgentKind, Candidate, DomainProfile

        fat = DomainProfile(
            kind=AgentKind.WEB,
            available=True,
            summary="test",
            structure_notes="x",
            mapped_sources=[
                Candidate(locator=f"https://x.test/{i}", title=str(i)) for i in range(80)
            ],
            mapped_total=80,
        )
        payload = profiles_for_planner([fat], mapped_cap=60)
        self.assertEqual(payload[0]["mapped_total"], 80)
        self.assertEqual(len(payload[0]["mapped_sources"]), 60)
        self.assertTrue(payload[0]["mapped_sources_truncated"])


if __name__ == "__main__":
    unittest.main()
