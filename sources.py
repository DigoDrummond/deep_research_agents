"""Source domains: describe / search / fetch.

Sprint 4: WEB describe/search/fetch + DomainRegistry.
Sprint 5: multi-query Describe maps dozens–100+ candidate URLs before Plan.
"""

from __future__ import annotations

import os
import re
from abc import ABC, abstractmethod
from collections import Counter
from datetime import date
from typing import Any
from urllib.parse import urlparse

import requests

from contracts import (
    AgentKind,
    Candidate,
    Clarification,
    Complexity,
    DomainProfile,
    PLANNER_MAPPED_SOURCES_CAP,
    SourceQuality,
    SourceRef,
    describe_budget_for,
)

DEFAULT_FIRECRAWL_API_URL = "http://localhost:3002"
WEB_ALLOWED_TOOLS = ["firecrawl_search", "firecrawl_scrape"]
# Firecrawl search usually caps ~20 per call; use that to fill the map faster.
_RECON_LIMIT_PER_QUERY = 20

_PRIMARY_HOST_HINTS = (
    "wikipedia.org",
    "gov",
    "edu",
    "who.int",
    "oecd.org",
    "imf.org",
    "worldbank.org",
    "un.org",
    "europa.eu",
    "nature.com",
    "science.org",
    "nih.gov",
    "cdc.gov",
)


class SourceDomain(ABC):
    kind: AgentKind

    @abstractmethod
    def describe(
        self,
        query: str,
        *,
        scope: str | None = None,
        as_of: str | None = None,
        angles: list[str] | None = None,
        complexity: Complexity = Complexity.exploratory,
    ) -> DomainProfile:
        ...

    @abstractmethod
    def search(self, query: str, limit: int = 5) -> list[Candidate]:
        ...

    @abstractmethod
    def fetch(self, locator: str) -> tuple[str, SourceRef]:
        ...


def _api_base() -> str:
    return os.environ.get("FIRECRAWL_API_URL", DEFAULT_FIRECRAWL_API_URL).rstrip("/")


def _short_recon_query(text: str, max_words: int = 4) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if not cleaned:
        return "overview"
    words = cleaned.split(" ")
    return " ".join(words[:max_words])


def _host(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().removeprefix("www.")
    except Exception:
        return ""


def _looks_primary(host: str) -> bool:
    h = host.lower()
    if h.endswith(".gov") or ".gov." in h:
        return True
    if h.endswith(".edu") or ".edu." in h:
        return True
    return any(hint in h for hint in _PRIMARY_HOST_HINTS)


def build_recon_queries(
    query: str,
    *,
    angles: list[str] | None = None,
    max_queries: int,
) -> list[str]:
    """Short breadth-first recon queries from the clarification (no LLM)."""
    seen: set[str] = set()
    out: list[str] = []

    def add(raw: str, max_words: int = 4) -> None:
        if len(out) >= max_queries:
            return
        q = _short_recon_query(raw, max_words=max_words)
        key = q.casefold()
        if key in seen or not q:
            return
        seen.add(key)
        out.append(q)

    add(query, max_words=4)
    for angle in angles or []:
        add(angle, max_words=4)
    # Slightly longer variant of the main query if we still have slots.
    add(query, max_words=6)
    if len(out) < max_queries and angles:
        for angle in angles:
            add(f"{_short_recon_query(query, 2)} {_short_recon_query(angle, 2)}", max_words=4)
    return out[:max_queries]


class WebDomain(SourceDomain):
    kind = AgentKind.WEB

    def __init__(self, api_url: str | None = None, timeout_s: float = 75.0):
        self.api_url = (api_url or _api_base()).rstrip("/")
        self.timeout_s = timeout_s

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.api_url}{path}"
        resp = requests.post(url, json=body, timeout=self.timeout_s)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise RuntimeError(f"unexpected Firecrawl response type: {type(data)}")
        return data

    def search(self, query: str, limit: int = 5) -> list[Candidate]:
        payload = self._post(
            "/v2/search",
            {"query": query, "limit": max(1, min(int(limit), 20))},
        )
        return self._candidates_from_search(payload)

    def fetch(self, locator: str) -> tuple[str, SourceRef]:
        payload = self._post(
            "/v2/scrape",
            {
                "url": locator,
                "formats": ["markdown"],
                "proxy": "basic",
                "onlyMainContent": True,
                "timeout": 60000,
            },
        )
        if not payload.get("success"):
            raise RuntimeError(payload.get("error") or "scrape failed")
        data = payload.get("data") or {}
        markdown = data.get("markdown") or ""
        meta = data.get("metadata") or {}
        title = meta.get("title") or meta.get("ogTitle") or locator
        host = _host(locator)
        quality = SourceQuality.primary if _looks_primary(host) else SourceQuality.unknown
        ref = SourceRef(
            domain=AgentKind.WEB,
            locator=locator,
            title=str(title)[:300],
            collected_at=date.today().isoformat(),
            published_at=None,
            quality=quality,
        )
        return str(markdown), ref

    def describe(
        self,
        query: str,
        *,
        scope: str | None = None,
        as_of: str | None = None,
        angles: list[str] | None = None,
        complexity: Complexity = Complexity.exploratory,
    ) -> DomainProfile:
        budget = describe_budget_for(complexity)
        recon_queries = build_recon_queries(
            query, angles=angles, max_queries=budget.max_queries
        )
        gaps: list[str] = [
            "Self-hosted Firecrawl has no Fire-engine anti-bot; hard sites may fail.",
            "Local /search may be rate-limited or empty; treat mapped URLs as candidates, not proof.",
            "Describe only catalogs sources; subagents scrape and emit Findings.",
        ]
        if as_of:
            gaps.append(f"Evidence after as_of={as_of} must be discarded by workers.")
        if scope:
            gaps.append(f"Stay inside agreed scope: {scope[:200]}")

        mapped: list[Candidate] = []
        seen_urls: set[str] = set()
        queries_ok = 0
        queries_failed = 0

        for recon in recon_queries:
            if len(mapped) >= budget.max_sources:
                break
            remaining = budget.max_sources - len(mapped)
            limit = min(_RECON_LIMIT_PER_QUERY, remaining)
            try:
                payload = self._post(
                    "/v2/search",
                    {"query": recon, "limit": limit},
                )
                batch = self._candidates_from_search(payload)
                queries_ok += 1
                for cand in batch:
                    key = cand.locator.rstrip("/").casefold()
                    if key in seen_urls:
                        continue
                    seen_urls.add(key)
                    mapped.append(cand)
                    if len(mapped) >= budget.max_sources:
                        break
            except Exception as exc:  # noqa: BLE001 — describe must not kill the pipeline
                queries_failed += 1
                gaps.append(
                    f"Recon query {recon!r} failed ({type(exc).__name__}: {exc})."
                )

        hosts = Counter(_host(c.locator) for c in mapped if _host(c.locator))
        dominant = [h for h, _ in hosts.most_common(12)]
        samples = [c.locator for c in mapped[:8]]
        primary = [h for h in dominant if _looks_primary(h)]

        if not mapped:
            gaps.append("Recon search returned no results; plan without mapped sources.")
            summary = (
                f"Open web Describe found 0 sources "
                f"({queries_ok} ok / {queries_failed} failed queries: {recon_queries})."
            )
        else:
            summary = (
                f"Mapped {len(mapped)} sources across {len(dominant)} hosts "
                f"via {queries_ok} recon queries (complexity={complexity.value}). "
                f"Top hosts: {', '.join(dominant[:5]) or '(none)'}. "
                f"Likely primary: {', '.join(primary[:4]) or '(none spotted)'}."
            )

        hints = [
            "Start with 2–4 word queries, then narrow (Describe already mapped the landscape).",
            "Prefer primary/official hosts (.gov, .edu, wikipedia, standards orgs) over SEO aggregators.",
            "Prefer scraping URLs from mapped_sources / dominant_hosts before inventing new searches.",
            "Search then scrape promising URLs; do not invent claims without a locator.",
            "Call independent searches in parallel when tools allow.",
        ]
        if dominant:
            hints.append(
                "When relevant, prefer these recon hosts: " + ", ".join(dominant[:8])
            )

        return DomainProfile(
            kind=AgentKind.WEB,
            available=True,
            summary=summary,
            structure_notes=(
                "Unstructured public pages. No fixed schema. "
                "Tools: firecrawl_search (find URLs) then firecrawl_scrape (markdown). "
                "Local scrape uses proxy=basic only. "
                "mapped_sources is a breadth-first catalog from Describe; not yet scraped."
            ),
            search_hints=hints,
            known_gaps=gaps,
            dominant_hosts=dominant,
            sample_locators=samples,
            allowed_tools=list(WEB_ALLOWED_TOOLS),
            mapped_sources=mapped,
            mapped_total=len(mapped),
        )

    @staticmethod
    def _candidates_from_search(payload: dict[str, Any]) -> list[Candidate]:
        if payload.get("success") is False:
            return []
        data = payload.get("data")
        rows: list[Any] = []
        if isinstance(data, list):
            rows = data
        elif isinstance(data, dict):
            for key in ("web", "news", "results"):
                val = data.get(key)
                if isinstance(val, list):
                    rows = val
                    break
        out: list[Candidate] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            locator = row.get("url") or row.get("link") or ""
            if not locator:
                continue
            title = row.get("title") or locator
            snippet = row.get("description") or row.get("snippet") or ""
            out.append(
                Candidate(
                    locator=str(locator),
                    title=str(title)[:300],
                    snippet=str(snippet)[:500],
                    domain=AgentKind.WEB,
                )
            )
        return out


class DomainRegistry:
    def __init__(self, domains: list[SourceDomain] | None = None):
        self._domains: dict[AgentKind, SourceDomain] = {}
        for domain in domains or []:
            self.register(domain)

    def register(self, domain: SourceDomain) -> None:
        self._domains[domain.kind] = domain

    def get(self, kind: AgentKind) -> SourceDomain | None:
        return self._domains.get(kind)

    def registered_kinds(self) -> list[AgentKind]:
        return list(self._domains.keys())

    def describe_all(self, clarification: Clarification) -> list[DomainProfile]:
        query = clarification.original_query or clarification.interpretation
        profiles: list[DomainProfile] = []
        for _kind, domain in self._domains.items():
            profile = domain.describe(
                query,
                scope=clarification.scope,
                as_of=clarification.as_of,
                angles=clarification.angles_considered,
                complexity=clarification.complexity,
            )
            profiles.append(profile)
        return profiles


def default_registry() -> DomainRegistry:
    """Session registry: WEB only until later sprints register more domains."""
    return DomainRegistry([WebDomain()])


def profiles_for_planner(
    profiles: list[DomainProfile],
    *,
    mapped_cap: int | None = None,
) -> list[dict[str, Any]]:
    """Serialize profiles for the planner LLM; truncate mapped_sources to save context."""
    cap = PLANNER_MAPPED_SOURCES_CAP if mapped_cap is None else mapped_cap
    out: list[dict[str, Any]] = []
    for profile in profiles:
        data = profile.model_dump()
        full = data.get("mapped_sources") or []
        total = int(data.get("mapped_total") or len(full))
        data["mapped_total"] = total
        if len(full) > cap:
            data["mapped_sources"] = full[:cap]
            data["mapped_sources_truncated"] = True
        else:
            data["mapped_sources_truncated"] = False
        out.append(data)
    return out
