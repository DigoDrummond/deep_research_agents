"""LLM chat helpers. Active provider: Vertex AI via LiteLLM.

To switch back to OpenAI: comment the Vertex block in chat_json and uncomment
the OpenAI block (same pattern as the old Hugging Face toggles).
"""

from __future__ import annotations

import json
import os
from typing import Any

import litellm

from token_meter import install_litellm_callback

# from openai import OpenAI

DEFAULT_VERTEX_PROJECT = "deepdocs-494218"
DEFAULT_VERTEX_LOCATION = "us-central1"


def vertex_project() -> str:
    return (
        os.environ.get("VERTEXAI_PROJECT")
        or os.environ.get("VERTEX_PROJECT")
        or DEFAULT_VERTEX_PROJECT
    )


def vertex_location() -> str:
    return (
        os.environ.get("VERTEXAI_LOCATION")
        or os.environ.get("VERTEX_LOCATION")
        or DEFAULT_VERTEX_LOCATION
    )


def ensure_vertex_env() -> None:
    """Populate env aliases LiteLLM / smolagents expect when unset."""
    project = vertex_project()
    location = vertex_location()
    os.environ.setdefault("VERTEXAI_PROJECT", project)
    os.environ.setdefault("VERTEX_PROJECT", project)
    os.environ.setdefault("VERTEXAI_LOCATION", location)
    os.environ.setdefault("VERTEX_LOCATION", location)


ensure_vertex_env()
install_litellm_callback()


def _schema_body(json_schema_wrapper: dict[str, Any]) -> dict[str, Any]:
    """Accept OpenAI-style {name, strict, schema} or a raw JSON Schema object."""
    if "schema" in json_schema_wrapper and isinstance(json_schema_wrapper["schema"], dict):
        return json_schema_wrapper["schema"]
    return json_schema_wrapper


def chat_json(
    model_id: str,
    messages: list[dict[str, str]],
    json_schema_wrapper: dict[str, Any],
) -> dict[str, Any]:
    """One chat completion that must return a JSON object matching the schema."""
    schema = _schema_body(json_schema_wrapper)

    # --- Vertex AI via LiteLLM (active) ---
    completion = litellm.completion(
        model=model_id,
        messages=messages,
        response_format={"type": "json_object", "response_schema": schema},
        vertex_project=vertex_project(),
        vertex_location=vertex_location(),
    )
    content = completion.choices[0].message.content
    if not content:
        raise RuntimeError(f"empty completion from {model_id}")
    return json.loads(content)

    # --- OpenAI (comentado — descomente e comente o bloco Vertex acima para voltar) ---
    # client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    # completion = client.chat.completions.create(
    #     model=model_id,
    #     messages=messages,
    #     response_format={
    #         "type": "json_schema",
    #         "json_schema": json_schema_wrapper,
    #     },
    # )
    # content = completion.choices[0].message.content
    # if not content:
    #     raise RuntimeError(f"empty completion from {model_id}")
    # return json.loads(content)
