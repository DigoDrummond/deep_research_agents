"""On-disk artifact store (sprint 6.3): the plan, every SubTaskResult, and the
raw firecrawl tool observations a worker actually saw survive past a single
`coordinator.run()` call.

Two things move here, matching section 6.3 of docs/plano-agentes-pesquisa-damas.md:

1. `save_plan` / `save_subtask_result` — the `ResearchPlan` and every
   `SubTaskResult` (`Finding[]` included) are written to
   `memory/<run_id>/{plan.json, <subtask_id>.json}`. `save_subtask_result`
   returns an `ArtifactRef` with `path` filled and a deterministic <=3-line
   `summary` — no LLM call, so the same `SubTaskResult` always yields the
   same summary.

2. `extract_documents` / `save_documents` — the raw `firecrawl_search` /
   `firecrawl_scrape` / `firecrawl_map` tool calls and their observations,
   pulled out of a worker's own `smolagents` memory after `.run()` and
   written to `memory/<run_id>/<subtask_id>.docs.json`. This is what
   upgrades `citations.py`'s anchor from `Finding.claim` (a proxy the
   subagent wrote about itself) to the literal fetched text — see
   docs/plano... section 12.5, sprint 6.4's "Falta".

Deliberately NOT done here: swapping what the coordinator sees in
`run_wave`'s return value from the full `Finding[]` to summary-only. The
current coordinator also writes the final report in that same tool-calling
loop — Run and Synthesize are not yet split into separate steps (see
docs/plano... section 12.6's note on pulling that orchestration into
main.py). Hiding `Finding[]` from the coordinator now would starve that
synthesis of the claims and sources it needs to write the report, trading
one regression for another. This waits for the Run/Synthesize split
(sprint 7); today `save_subtask_result`'s `ArtifactRef` is additive
(attached to `SubTaskResult.artifacts`), not a replacement.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from contracts import ArtifactRef, ResearchPlan, SubTaskResult

MEMORY_ROOT = Path(__file__).resolve().parent / "memory"

# Tool names whose observations are worth persisting as documents. Only
# firecrawl_scrape observations are keyed by a URL a Finding.SourceRef.locator
# can actually match; search/map are kept too (useful for sprint 9 tracing)
# but citations.py does not try to match claims against them today.
DOCUMENT_TOOLS = frozenset({"firecrawl_search", "firecrawl_scrape", "firecrawl_map"})


def _safe_run_id(run_id: str) -> str:
    slug = re.sub(r"[^0-9A-Za-z_-]+", "_", run_id.strip())
    return slug or "run"


def _run_dir(run_id: str) -> Path:
    path = MEMORY_ROOT / _safe_run_id(run_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_plan(run_id: str, plan: ResearchPlan) -> Path:
    path = _run_dir(run_id) / "plan.json"
    path.write_text(
        json.dumps(plan.model_dump(exclude_none=True), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _summarize(result: SubTaskResult) -> str:
    """Deterministic <=3-line summary: same SubTaskResult -> same summary,
    every time, with no LLM call. This is the text the architecture wants
    the coordinator reading instead of the full Finding[] once Run and
    Synthesize are split (see module docstring).
    """
    lines: list[str] = []
    if result.findings:
        preview = "; ".join(finding.claim[:120] for finding in result.findings[:2])
        extra = f" (+{len(result.findings) - 2} outro(s))" if len(result.findings) > 2 else ""
        lines.append(f"{len(result.findings)} achado(s): {preview}{extra}")
    else:
        lines.append("Nenhum achado.")
    if result.gaps:
        lines.append(f"{len(result.gaps)} lacuna(s): {result.gaps[0][:120]}")
    return "\n".join(lines[:3])


def save_subtask_result(run_id: str, result: SubTaskResult) -> ArtifactRef:
    """Persist the full SubTaskResult and return a reference to it. Written
    without any ArtifactRef attached to itself — the caller decides whether
    to fold this ref into `result.artifacts` before persisting/returning it
    elsewhere, so the on-disk copy is always the plain worker output.
    """
    path = _run_dir(run_id) / f"{result.subtask_id}.json"
    path.write_text(
        json.dumps(result.model_dump(exclude_none=True), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return ArtifactRef(
        subtask_id=result.subtask_id,
        type="SubTaskResult",
        summary=_summarize(result),
        path=str(path.relative_to(MEMORY_ROOT.parent)),
    )


def extract_documents(memory_steps: list[Any]) -> list[dict[str, Any]]:
    """Pull firecrawl tool calls + their observations out of a worker's
    smolagents memory after `.run()`. Best-effort: any unexpected shape is
    skipped rather than raised — a missing document is a gap, not a crash.
    """
    documents: list[dict[str, Any]] = []
    for step in memory_steps:
        tool_calls = getattr(step, "tool_calls", None)
        observation = getattr(step, "observations", None)
        if not tool_calls or not observation:
            continue
        for call in tool_calls:
            name = getattr(call, "name", None)
            if name not in DOCUMENT_TOOLS:
                continue
            arguments = getattr(call, "arguments", None)
            locator = arguments.get("url") if isinstance(arguments, dict) else None
            documents.append(
                {
                    "tool": name,
                    "locator": locator,
                    "arguments": arguments if isinstance(arguments, (dict, str)) else str(arguments),
                    "content": observation,
                }
            )
    return documents


def save_documents(run_id: str, subtask_id: str, documents: list[dict[str, Any]]) -> Path | None:
    if not documents:
        return None
    path = _run_dir(run_id) / f"{subtask_id}.docs.json"
    path.write_text(json.dumps(documents, indent=2, ensure_ascii=False), encoding="utf-8")
    return path
