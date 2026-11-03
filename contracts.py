from __future__ import annotations

import re
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class AgentKind(str, Enum):
    WEB = "WEB"
    SOCIAL = "SOCIAL"
    DOCS = "DOCS"
    TIMESERIES = "TIMESERIES"
    TABULAR = "TABULAR"


IMPLEMENTED_KINDS = frozenset({AgentKind.WEB})


class Complexity(str, Enum):
    lookup = "lookup"
    comparison = "comparison"
    exploratory = "exploratory"
    deep = "deep"


class SourceQuality(str, Enum):
    primary = "primary"
    secondary = "secondary"
    unknown = "unknown"


class Confidence(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


class Budget(BaseModel):
    subagents: int
    calls_per_subagent: int
    parallelism: int

# Para versão final somente
# BUDGETS: dict[Complexity, Budget] = {
#     Complexity.lookup: Budget(subagents=1, calls_per_subagent=5, parallelism=1),
#     Complexity.comparison: Budget(subagents=4, calls_per_subagent=12, parallelism=4),
#     Complexity.exploratory: Budget(subagents=6, calls_per_subagent=15, parallelism=4),
#     Complexity.deep: Budget(subagents=12, calls_per_subagent=20, parallelism=5),
# }

BUDGETS: dict[Complexity, Budget] = {
    Complexity.lookup: Budget(subagents=1, calls_per_subagent=4, parallelism=1),
    Complexity.comparison: Budget(subagents=2, calls_per_subagent=6, parallelism=2),
    Complexity.exploratory: Budget(subagents=3, calls_per_subagent=8, parallelism=3),
    Complexity.deep: Budget(subagents=4, calls_per_subagent=10, parallelism=4),
}


class DescribeBudget(BaseModel):
    """How wide the Describe source-map recon runs before Plan."""

    max_queries: int
    max_sources: int


DESCRIBE_BUDGETS: dict[Complexity, DescribeBudget] = {
    Complexity.lookup: DescribeBudget(max_queries=2, max_sources=25),
    Complexity.comparison: DescribeBudget(max_queries=4, max_sources=50),
    Complexity.exploratory: DescribeBudget(max_queries=6, max_sources=80),
    Complexity.deep: DescribeBudget(max_queries=8, max_sources=120),
}

# Cap how many mapped URLs are sent to the planner LLM (full map may be larger).
PLANNER_MAPPED_SOURCES_CAP = 60


def describe_budget_for(complexity: Complexity) -> DescribeBudget:
    return DESCRIBE_BUDGETS[complexity]

class SourceRef(BaseModel):
    domain: AgentKind
    locator: str
    title: str
    collected_at: str | None = None
    published_at: str | None = None
    quality: SourceQuality = SourceQuality.unknown


class Candidate(BaseModel):
    locator: str
    title: str
    snippet: str = ""
    domain: AgentKind = AgentKind.WEB


class DomainProfile(BaseModel):
    kind: AgentKind
    available: bool
    summary: str
    structure_notes: str
    search_hints: list[str] = Field(default_factory=list)
    known_gaps: list[str] = Field(default_factory=list)
    dominant_hosts: list[str] = Field(default_factory=list)
    sample_locators: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    mapped_sources: list[Candidate] = Field(default_factory=list)
    mapped_total: int = 0


class Finding(BaseModel):
    claim: str
    sources: list[SourceRef] = Field(min_length=1)
    confidence: Confidence
    caveats: str = ""


class ArtifactRef(BaseModel):
    subtask_id: str
    type: str
    summary: str
    path: str | None = None


class Citation(BaseModel):
    """Produced by CitationAgent (sprint 6.4), never by the synthesizer or a
    subagent. Anchors one claim in the FINAL report to a source. `anchor` is
    the strongest evidence text available for that source today: the
    Finding.claim that carried the SourceRef. Once memory.py (sprint 6.3)
    persists raw fetched documents, `anchor` should become a literal excerpt
    from that document instead.
    """

    claim: str
    source: SourceRef
    anchor: str


class SubTask(BaseModel):
    id: str
    title: str
    objective: str
    output_format: str
    kind: AgentKind = AgentKind.WEB
    sibling_boundaries: str
    success_criteria: str
    max_calls: int = 5
    dataframes: list[dict[str, Any]] | None = None


class DomainSkip(BaseModel):
    kind: AgentKind
    reason: str


class SubTaskResult(BaseModel):
    subtask_id: str
    findings: list[Finding] = Field(default_factory=list)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    calls_used: int = 0


class Clarification(BaseModel):
    original_query: str
    interpretation: str
    scope: str
    as_of: str | None = None
    complexity: Complexity
    angles_considered: list[str]
    questions: list[str] = Field(default_factory=list, max_length=4)
    answers: list[str] = Field(default_factory=list)

    @field_validator("as_of", mode="before")
    @classmethod
    def empty_as_of_is_none(cls, value: Any) -> Any:
        if value == "":
            return None
        return value


class ResearchPlan(BaseModel):
    original_query: str
    interpretation: str
    scope: str
    as_of: str | None = None
    complexity: Complexity
    angles_considered: list[str]
    domains_skipped: list[DomainSkip]
    subtasks: list[SubTask]

    @field_validator("as_of", mode="before")
    @classmethod
    def empty_as_of_is_none(cls, value: Any) -> Any:
        if value == "":
            return None
        return value


def budget_for(complexity: Complexity) -> Budget:
    return BUDGETS[complexity]


def pin_as_of(clarification: Clarification, as_of: str | None) -> Clarification:
    """If the caller set as_of (evals), the LLM cannot override it."""
    if as_of is None or as_of == "":
        return clarification
    return clarification.model_copy(update={"as_of": as_of})


def apply_clarification(plan: ResearchPlan, clarification: Clarification) -> ResearchPlan:
    """Harness: the planner cannot skip the agreed Ask fields."""
    return plan.model_copy(
        update={
            "original_query": clarification.original_query,
            "interpretation": clarification.interpretation,
            "scope": clarification.scope,
            "as_of": clarification.as_of,
            "complexity": clarification.complexity,
            "angles_considered": clarification.angles_considered,
        }
    )


def sanitize_subtask_id(value: str, used: set[str] | None = None) -> str:
    """smolagents requires agent names to be valid Python identifiers (no hyphens)."""
    slug = re.sub(r"[^0-9A-Za-z_]+", "_", value.strip())
    slug = re.sub(r"_+", "_", slug).strip("_")
    if not slug:
        slug = "subtask"
    if slug[0].isdigit():
        slug = f"t_{slug}"
    if used is None:
        return slug
    base = slug
    n = 2
    while slug in used:
        slug = f"{base}_{n}"
        n += 1
    used.add(slug)
    return slug


def worker_name(subtask_id: str) -> str:
    name = f"subagent_{sanitize_subtask_id(subtask_id)}"
    return name if name.isidentifier() else "subagent_worker"


def clamp_plan(plan: ResearchPlan) -> ResearchPlan:
    """Harness rules the planner cannot skip: cap count, force WEB, fill max_calls."""
    budget = budget_for(plan.complexity)
    skipped = list(plan.domains_skipped)
    subtasks: list[SubTask] = []
    used_ids: set[str] = set()
    id_map: dict[str, str] = {}
    for raw in plan.subtasks:
        kind = raw.kind if raw.kind in IMPLEMENTED_KINDS else AgentKind.WEB
        if raw.kind not in IMPLEMENTED_KINDS:
            skipped.append(
                DomainSkip(
                    kind=raw.kind,
                    reason=f"{raw.kind.value} is not registered; subtask {raw.id} forced to WEB",
                )
            )
        new_id = sanitize_subtask_id(raw.id, used_ids)
        id_map[raw.id] = new_id
        subtasks.append(
            raw.model_copy(
                update={
                    "id": new_id,
                    "kind": kind,
                    "max_calls": budget.calls_per_subagent,
                    "dataframes": None,
                }
            )
        )
        if len(subtasks) >= budget.subagents:
            break

    rewritten: list[SubTask] = []
    for task in subtasks:
        boundaries = task.sibling_boundaries
        for old_id, new_id in id_map.items():
            if old_id != new_id:
                boundaries = boundaries.replace(old_id, new_id)
        rewritten.append(
            task
            if boundaries == task.sibling_boundaries
            else task.model_copy(update={"sibling_boundaries": boundaries})
        )
    subtasks = rewritten

    seen: set[tuple[AgentKind, str]] = set()
    unique_skips: list[DomainSkip] = []
    for item in skipped:
        key = (item.kind, item.reason)
        if key in seen:
            continue
        seen.add(key)
        unique_skips.append(item)

    return plan.model_copy(update={"subtasks": subtasks, "domains_skipped": unique_skips})


def compact_result(result: SubTaskResult) -> dict[str, Any]:
    return result.model_dump(exclude_none=True)


def parse_subtask_result(raw: Any, subtask_id: str) -> SubTaskResult:
    """Parse a worker return into SubTaskResult. Unparseable output becomes a declared gap."""
    text = raw if isinstance(raw, str) else str(raw)
    payload = _extract_json(text)
    if payload is None:
        return SubTaskResult(
            subtask_id=subtask_id,
            findings=[],
            gaps=["Worker returned unparseable output instead of SubTaskResult JSON."],
            calls_used=0,
        )
    if isinstance(payload, dict) and "subtask_id" not in payload:
        payload["subtask_id"] = subtask_id
    try:
        result = SubTaskResult.model_validate(payload)
    except Exception:
        return SubTaskResult(
            subtask_id=subtask_id,
            findings=[],
            gaps=["Worker JSON did not match SubTaskResult."],
            calls_used=0,
        )
    if result.subtask_id != subtask_id:
        result = result.model_copy(update={"subtask_id": subtask_id})
    return result


def _extract_json(text: str) -> Any | None:
    import json
    import re

    stripped = text.strip()
    candidates = [stripped]
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", stripped, re.DOTALL)
    if fenced:
        candidates.insert(0, fenced.group(1))
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end > start:
        candidates.append(stripped[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


# OpenAI strict json_schema (additionalProperties: false, every key required).
# answers is filled in Python, not by the LLM.
CLARIFICATION_JSON_SCHEMA = {
    "name": "clarification",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "original_query": {"type": "string"},
            "interpretation": {"type": "string"},
            "scope": {"type": "string"},
            "as_of": {"type": ["string", "null"]},
            "complexity": {
                "type": "string",
                "enum": ["lookup", "comparison", "exploratory", "deep"],
            },
            "angles_considered": {"type": "array", "items": {"type": "string"}},
            "questions": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 4,
            },
        },
        "required": [
            "original_query",
            "interpretation",
            "scope",
            "as_of",
            "complexity",
            "angles_considered",
            "questions",
        ],
    },
}

RESEARCH_PLAN_JSON_SCHEMA = {
    "name": "researchPlan",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "original_query": {"type": "string"},
            "interpretation": {"type": "string"},
            "scope": {"type": "string"},
            "as_of": {"type": ["string", "null"]},
            "complexity": {
                "type": "string",
                "enum": ["lookup", "comparison", "exploratory", "deep"],
            },
            "angles_considered": {"type": "array", "items": {"type": "string"}},
            "domains_skipped": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": ["WEB", "SOCIAL", "DOCS", "TIMESERIES", "TABULAR"],
                        },
                        "reason": {"type": "string"},
                    },
                    "required": ["kind", "reason"],
                },
            },
            "subtasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "id": {"type": "string"},
                        "title": {"type": "string"},
                        "objective": {
                            "type": "string",
                            "description": "One imperative sentence.",
                        },
                        "output_format": {"type": "string"},
                        "kind": {
                            "type": "string",
                            "enum": ["WEB", "SOCIAL", "DOCS", "TIMESERIES", "TABULAR"],
                        },
                        "sibling_boundaries": {
                            "type": "string",
                            "description": "Name sister subtasks this one must not duplicate.",
                        },
                        "success_criteria": {"type": "string"},
                        "max_calls": {"type": "integer"},
                        "dataframes": {"type": "null"},
                    },
                    "required": [
                        "id",
                        "title",
                        "objective",
                        "output_format",
                        "kind",
                        "sibling_boundaries",
                        "success_criteria",
                        "max_calls",
                        "dataframes",
                    ],
                },
            },
        },
        "required": [
            "original_query",
            "interpretation",
            "scope",
            "as_of",
            "complexity",
            "angles_considered",
            "domains_skipped",
            "subtasks",
        ],
    },
}

_SOURCE_REF_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "domain": {
            "type": "string",
            "enum": ["WEB", "SOCIAL", "DOCS", "TIMESERIES", "TABULAR"],
        },
        "locator": {"type": "string"},
        "title": {"type": "string"},
        "collected_at": {"type": ["string", "null"]},
        "published_at": {"type": ["string", "null"]},
        "quality": {"type": "string", "enum": ["primary", "secondary", "unknown"]},
    },
    "required": ["domain", "locator", "title", "collected_at", "published_at", "quality"],
}

CITATION_JSON_SCHEMA = {
    "name": "citationVerdict",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "revised_report": {
                "type": "string",
                "description": (
                    "Full corrected markdown report: same as the draft, except "
                    "orphaned claims are demoted to 'what we do not know' or removed."
                ),
            },
            "citations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "claim": {"type": "string"},
                        "source": _SOURCE_REF_JSON_SCHEMA,
                        "anchor": {"type": "string"},
                    },
                    "required": ["claim", "source", "anchor"],
                },
            },
            "orphaned_claims": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Verbatim text of claims demoted or removed for lacking an anchor.",
            },
        },
        "required": ["revised_report", "citations", "orphaned_claims"],
    },
}
