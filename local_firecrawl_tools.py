"""Narrow Firecrawl MCP tools for self-hosted API (no cloud-only schema fields)."""

from __future__ import annotations

import json
from typing import Any

from smolagents import Tool

SCRAPE_NAME = "firecrawl_scrape"
SEARCH_NAME = "firecrawl_search"
MAP_NAME = "firecrawl_map"

# Keep tool observations small so multi-step agents do not blow Vertex/LiteLLM
# context (seen ~200k input tokens before the next step failed).
SCRAPE_MAX_CHARS = 8_000
SEARCH_MAX_CHARS = 6_000
MAP_MAX_CHARS = 6_000


def _index_by_name(mcp_tools: list[Tool]) -> dict[str, Tool]:
    return {tool.name: tool for tool in mcp_tools}


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        return str(value)


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    keep = max_chars // 2
    return (
        text[:keep]
        + f"\n...[truncated to {max_chars} chars for agent context]...\n"
        + text[-keep:]
    )


class LocalFirecrawlScrapeTool(Tool):
    name = SCRAPE_NAME
    description = (
        "Scrape a URL via self-hosted Firecrawl and return markdown (and metadata). "
        "Pass only the URL; optional formats defaults to [\"markdown\"]. "
        "Do not pass extract, proxy, redactPII, actions, or queryOptions. "
        f"Output is truncated to ~{SCRAPE_MAX_CHARS} characters."
    )
    inputs = {
        "url": {
            "type": "string",
            "description": "Absolute http(s) URL to scrape",
        },
        "formats": {
            "type": "array",
            "description": 'Output formats; use ["markdown"] only',
            "nullable": True,
        },
    }
    output_type = "string"

    def __init__(self, mcp_scrape: Tool):
        self._mcp = mcp_scrape
        self.is_initialized = True

    def forward(self, url: str, formats: list[str] | None = None) -> Any:
        fmt = formats if isinstance(formats, list) and formats else ["markdown"]
        raw = self._mcp(
            url=url,
            formats=fmt,
            proxy="basic",
            onlyMainContent=True,
        )
        return _truncate(_as_text(raw), SCRAPE_MAX_CHARS)


class LocalFirecrawlSearchTool(Tool):
    name = SEARCH_NAME
    description = (
        "Web search via self-hosted Firecrawl. Pass query and optional limit. "
        "Do not pass scrapeOptions or extract options. "
        f"Output is truncated to ~{SEARCH_MAX_CHARS} characters."
    )
    inputs = {
        "query": {
            "type": "string",
            "description": "Search query (keep short: 2–6 words on the first try)",
        },
        "limit": {
            "type": "integer",
            "description": "Max results (default 5)",
            "nullable": True,
        },
    }
    output_type = "string"

    def __init__(self, mcp_search: Tool):
        self._mcp = mcp_search
        self.is_initialized = True

    def forward(self, query: str, limit: int | None = None) -> Any:
        kwargs: dict[str, Any] = {"query": query}
        if limit is not None:
            kwargs["limit"] = int(limit)
        else:
            kwargs["limit"] = 5
        raw = self._mcp(**kwargs)
        return _truncate(_as_text(raw), SEARCH_MAX_CHARS)


class LocalFirecrawlMapTool(Tool):
    name = MAP_NAME
    description = (
        "Enumerate URLs indexed under one website via self-hosted Firecrawl, "
        "without fetching each page's content. Use this to see what pages a "
        "promising host actually has (e.g. a host from dominant_hosts) before "
        "choosing what to scrape, or to discover a page whose exact URL is "
        "unknown. Cheaper than search+scrape for exploring one site's structure. "
        "Do not pass proxy or other cloud-only options. "
        f"Output is truncated to ~{MAP_MAX_CHARS} characters."
    )
    inputs = {
        "url": {
            "type": "string",
            "description": "Absolute http(s) URL of the site (or a path within it) to map",
        },
        "search": {
            "type": "string",
            "description": "Optional term to narrow the returned URL list",
            "nullable": True,
        },
        "limit": {
            "type": "integer",
            "description": "Max URLs to return (default 100)",
            "nullable": True,
        },
    }
    output_type = "string"

    def __init__(self, mcp_map: Tool):
        self._mcp = mcp_map
        self.is_initialized = True

    def forward(self, url: str, search: str | None = None, limit: int | None = None) -> Any:
        kwargs: dict[str, Any] = {"url": url}
        if search:
            kwargs["search"] = search
        kwargs["limit"] = int(limit) if limit is not None else 100
        raw = self._mcp(**kwargs)
        return _truncate(_as_text(raw), MAP_MAX_CHARS)


def wrap_local_firecrawl_tools(mcp_tools: list[Tool]) -> list[Tool]:
    """Return scrape+search+map wrappers; raise if MCP tools are missing."""
    by_name = _index_by_name(mcp_tools)
    required = (SCRAPE_NAME, SEARCH_NAME, MAP_NAME)
    missing = [name for name in required if name not in by_name]
    if missing:
        available = ", ".join(sorted(by_name)) or "(none)"
        raise RuntimeError(
            "Local Firecrawl wrappers need MCP tools "
            f"{', '.join(required)}; missing: {missing}. "
            f"Available: {available}"
        )
    return [
        LocalFirecrawlScrapeTool(by_name[SCRAPE_NAME]),
        LocalFirecrawlSearchTool(by_name[SEARCH_NAME]),
        LocalFirecrawlMapTool(by_name[MAP_NAME]),
    ]
