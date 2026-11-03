"""CitationAgent (sprint 6.4): anchors report claims to Finding sources.

Runs after the coordinator's synthesis and before the report is persisted.
Section 7.1 of docs/plano-agentes-pesquisa-damas.md: `Finding.sources` says
where a subagent *thinks* it got a claim; this step says whether the *final*
report is still anchored. They are different jobs, run by a model with its
own context window, precisely because a synthesizer conflates confidence
with correctness.

Sprint 6.3 (memory.py) upgrade: when the harness also hands over the raw
`firecrawl_scrape` observations a worker saw (`documents`, keyed by URL),
each Finding's SourceRef gets a `fetched_excerpt` — the actual page text the
subagent read — so the LLM can check whether a claim's specific numbers
match what the source really says, not just that *some* source exists.
Without `documents` (or when a source's URL was never scraped, e.g. it came
from a search snippet), the anchor falls back to the Finding.claim itself —
the strongest proxy available, not a literal excerpt.
"""

from __future__ import annotations

import json

from contracts import CITATION_JSON_SCHEMA, Citation, Finding, SourceRef
from llm_clients import chat_json
from prompts import CITATION_AGENT_INSTRUCTIONS

MODEL_ID = "vertex_ai/gemini-2.5-pro"

# Cap per-document excerpt so a handful of matched pages cannot blow the
# citation call's context; SCRAPE_MAX_CHARS in local_firecrawl_tools.py
# already caps the raw observation, this caps it further per excerpt.
EXCERPT_MAX_CHARS = 3_000


def _scraped_by_locator(documents: list[dict] | None) -> dict[str, str]:
    by_locator: dict[str, str] = {}
    for doc in documents or []:
        if doc.get("tool") != "firecrawl_scrape":
            continue
        locator = doc.get("locator")
        content = doc.get("content")
        if locator and content and locator not in by_locator:
            by_locator[locator] = content[:EXCERPT_MAX_CHARS]
    return by_locator


def _findings_payload(findings: list[Finding], documents: list[dict] | None = None) -> str:
    scraped = _scraped_by_locator(documents)
    payload = []
    for finding in findings:
        item = finding.model_dump(exclude_none=True)
        for source in item.get("sources", []):
            excerpt = scraped.get(source.get("locator"))
            if excerpt:
                source["fetched_excerpt"] = excerpt
        payload.append(item)
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _format_bibliography(citations: list[Citation]) -> str:
    """Harness-appended, deterministic bibliography — not left to the LLM to
    remember. This is the exact failure mode sprint 6.1/6.2 validation found:
    Finding.sources existed but the coordinator's draft sometimes dropped the
    bibliography anyway. Citations here are the checked-in ground truth.
    """
    if not citations:
        return ""
    seen: set[str] = set()
    lines = ["", "---", "", "## Fontes citadas", ""]
    for citation in citations:
        source: SourceRef = citation.source
        locator = source.locator
        if locator in seen:
            continue
        seen.add(locator)
        title = source.title or locator
        if locator.startswith("http://") or locator.startswith("https://"):
            lines.append(f"- [{title}]({locator})")
        else:
            lines.append(f"- {title} ({locator})")
    return "\n".join(lines) + "\n"


def apply_citations(
    draft_report: str,
    findings: list[Finding],
    documents: list[dict] | None = None,
) -> tuple[str, list[Citation]]:
    """Anchor every factual claim in `draft_report` to a Finding's SourceRef.

    Returns (final_report, citations). `final_report` is the LLM's demoted/
    cleaned draft with a harness-built bibliography appended from
    `citations` — the bibliography does not depend on the LLM remembering to
    write one. If `findings` is empty there is nothing to anchor against, so
    the harness skips the LLM call and returns the draft unchanged: with zero
    evidence, demoting claims would be guessing, not verifying. `documents`
    (from memory.extract_documents, sprint 6.3) is optional; when present it
    lets a finding's SourceRef carry the actual fetched text.
    """
    if not findings:
        return draft_report, []

    print(f"Running CitationAgent over {len(findings)} finding(s)...")
    user_content = (
        'Draft report:\n"""\n'
        + draft_report.strip()
        + '\n"""\n\n'
        "Available findings (claim + at least one SourceRef each; a source may "
        "carry fetched_excerpt, the actual page text a subagent read):\n"
        + _findings_payload(findings, documents)
    )
    try:
        payload = chat_json(
            MODEL_ID,
            [
                {"role": "system", "content": CITATION_AGENT_INSTRUCTIONS.strip()},
                {"role": "user", "content": user_content},
            ],
            CITATION_JSON_SCHEMA,
        )
        citations = [Citation.model_validate(item) for item in payload.get("citations", [])]
        revised = payload.get("revised_report") or draft_report
        orphaned = payload.get("orphaned_claims") or []
    except Exception as exc:  # CitationAgent failing must not sink the whole run
        print(f"  CitationAgent failed ({type(exc).__name__}: {exc}); keeping draft unanchored")
        return draft_report, []

    print(f"  anchored {len(citations)} claim(s), demoted {len(orphaned)} orphaned claim(s)")
    final_report = revised.rstrip() + "\n" + _format_bibliography(citations)
    return final_report, citations
