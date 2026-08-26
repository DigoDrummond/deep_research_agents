import json
import os
from typing import Any

# from openai import OpenAI

from llm_clients import chat_json

# OpenAI (comentado — descomente e comente o bloco Vertex para voltar)
# JUDGE_MODEL_ID = "gpt-4.1"

# Vertex AI via LiteLLM (ativo)
JUDGE_MODEL_ID = "vertex_ai/gemini-2.5-pro"

JUDGE_SYSTEM_PROMPT = """
You are a single-call research-report judge. Score the REPORT against the QUERY
and, when present, EXPECTED and AS_OF. Do not research. Do not rewrite the report.

Return one JSON object. Scores are floats from 0.0 to 1.0.

Rubric:
- factual_accuracy: claims match the cited sources and well-established public facts.
  Invented numbers, fake companies treated as real, or contradictions with EXPECTED
  (when EXPECTED is not "insufficient_evidence") push this down.
- citation_accuracy: cited URLs/titles actually support the claims they sit next to.
  Decorative bibliographies that do not back the sentences score low.
- completeness: requested aspects of the QUERY are covered, or explicitly marked
  as unknown. Silence on a requested aspect scores low.
- source_quality: primary/official sources over SEO farms, aggregators, and blogs.
- tool_efficiency: the report looks like it used a reasonable number of sources
  rather than dumping an encyclopedia or citing nothing. If you cannot tell, use 0.5.
- as_of_respect: if AS_OF is set, any fact, figure, statute, or source dated after
  that day is a leak and this score must be 0.0. If AS_OF is empty, use 1.0.

Special cases:
- If EXPECTED is "insufficient_evidence": the correct behavior is to say there is
  not enough evidence. Asserting a specific fact as known is an automatic fail
  (passed=false, factual_accuracy at most 0.2).
- Point-in-time leaks (post-AS_OF evidence used as if contemporaneous) are an
  automatic fail.

passed is true only if the overall mean of the six scores is >= 0.6 AND none of
the automatic-fail rules triggered.

rationale: 3–8 sentences in the same language as the QUERY, citing the main
reasons for the scores.
"""

JUDGE_JSON_SCHEMA = {
    "name": "judgeScore",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "factual_accuracy": {"type": "number"},
            "citation_accuracy": {"type": "number"},
            "completeness": {"type": "number"},
            "source_quality": {"type": "number"},
            "tool_efficiency": {"type": "number"},
            "as_of_respect": {"type": "number"},
            "passed": {"type": "boolean"},
            "rationale": {"type": "string"},
        },
        "required": [
            "factual_accuracy",
            "citation_accuracy",
            "completeness",
            "source_quality",
            "tool_efficiency",
            "as_of_respect",
            "passed",
            "rationale",
        ],
    },
}

SCORE_KEYS = (
    "factual_accuracy",
    "citation_accuracy",
    "completeness",
    "source_quality",
    "tool_efficiency",
    "as_of_respect",
)


def _clamp(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, number))


def judge_report(query: dict, report: str) -> dict:
    """One LLM call. Returns scores 0–1, passed, rationale, and mean."""
    expected = query.get("expected")
    if expected is None:
        expected_text = "(none)"
    else:
        expected_text = str(expected).strip()

    as_of = query.get("as_of") or ""
    user_payload = (
        f"QUERY:\n{query['query'].strip()}\n\n"
        f"TAGS: {query.get('tags', [])}\n"
        f"AS_OF: {as_of or '(none)'}\n"
        f"EXPECTED:\n{expected_text}\n\n"
        f"REPORT:\n{report}"
    )

    # OpenAI (comentado):
    # client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    # completion = client.chat.completions.create(
    #     model=JUDGE_MODEL_ID,
    #     messages=[
    #         {"role": "system", "content": JUDGE_SYSTEM_PROMPT.strip()},
    #         {"role": "user", "content": user_payload},
    #     ],
    #     response_format={
    #         "type": "json_schema",
    #         "json_schema": JUDGE_JSON_SCHEMA,
    #     },
    # )
    # raw = json.loads(completion.choices[0].message.content)

    raw = chat_json(
        JUDGE_MODEL_ID,
        [
            {"role": "system", "content": JUDGE_SYSTEM_PROMPT.strip()},
            {"role": "user", "content": user_payload},
        ],
        JUDGE_JSON_SCHEMA,
    )
    scores = {key: _clamp(raw.get(key)) for key in SCORE_KEYS}
    mean = sum(scores.values()) / len(scores)
    return {
        **scores,
        "mean": round(mean, 4),
        "passed": bool(raw.get("passed")),
        "rationale": str(raw.get("rationale") or "").strip(),
        "model": JUDGE_MODEL_ID,
    }
