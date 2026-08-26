from stdio_utf8 import configure_utf8_stdio

configure_utf8_stdio()

from clarifier import clarify
from planner import generate_research_plan
from prompts import SUBAGENT_PROMPT_TEMPLATE, COORDINATOR_PROMPT_TEMPLATE
from smolagents import LiteLLMModel, ToolCallingAgent, MCPClient, tool, InferenceClientModel, OpenAIServerModel
import os
import json

from contracts import (
    IMPLEMENTED_KINDS,
    AgentKind,
    ResearchPlan,
    SubTask,
    budget_for,
    compact_result,
    parse_subtask_result,
    worker_name,
)
from local_firecrawl_tools import wrap_local_firecrawl_tools
from llm_clients import ensure_vertex_env
from sources import default_registry
from token_meter import meter

ensure_vertex_env()

MCP_URL = os.environ.get("FIRECRAWL_MCP_URL", "http://localhost:3000/mcp")

# Hugging Face Inference (comentado — reative se os créditos voltarem)
# COORDINATOR_MODEL_ID = "MiniMaxAI/MiniMax-M1-80k"
# SUBAGENT_MODEL_ID    = "MiniMaxAI/MiniMax-M1-80k"

# OpenAI (comentado — descomente e comente o bloco Vertex para voltar)
# COORDINATOR_MODEL_ID = "gpt-4.1"
# SUBAGENT_MODEL_ID = "gpt-4o"

# Vertex AI via LiteLLM (ativo)
COORDINATOR_MODEL_ID = "vertex_ai/gemini-2.5-pro"
SUBAGENT_MODEL_ID = "vertex_ai/gemini-2.5-flash"


def _as_of_label(plan: ResearchPlan) -> str:
    return plan.as_of or ""


def _subtask_prompt(plan: ResearchPlan, subtask: SubTask) -> str:
    return SUBAGENT_PROMPT_TEMPLATE.format(
        interpretation=plan.interpretation,
        as_of=_as_of_label(plan) or "(now)",
        subtask_id=subtask.id,
        subtask_title=subtask.title,
        objective=subtask.objective,
        output_format=subtask.output_format,
        sibling_boundaries=subtask.sibling_boundaries,
        success_criteria=subtask.success_criteria,
        max_calls=subtask.max_calls,
    )


def _planned_subtasks_json(plan: ResearchPlan) -> str:
    payload = [
        task.model_dump(exclude={"dataframes"}, exclude_none=True)
        for task in plan.subtasks
    ]
    return json.dumps(payload, indent=2, ensure_ascii=False)


def run_deep_research(
    user_query: str,
    *,
    interactive: bool = False,
    as_of: str | None = None,
) -> str:
    print("Running the deep research...")
    meter.reset()

    clarification = clarify(user_query, interactive=interactive, as_of=as_of)

    print("Describing source domains (breadth-first source map)...")
    profiles = default_registry().describe_all(clarification)
    for profile in profiles:
        print(
            f"  [{profile.kind.value}] mapped_total={profile.mapped_total} "
            f"hosts={len(profile.dominant_hosts)} available={profile.available}"
        )
        print(f"    {profile.summary[:240]}")

    plan = generate_research_plan(clarification, profiles=profiles)
    budget = budget_for(plan.complexity)

    print("Initializing Coordinator")
    print("Coordinator Model: ", COORDINATOR_MODEL_ID)
    print("Subagent Model: ", SUBAGENT_MODEL_ID)
    print(f"Budget: {budget.subagents} subagents x {budget.calls_per_subagent} calls")

    # coordinator_model = InferenceClientModel(
    #     model_id=COORDINATOR_MODEL_ID,
    #     api_key=os.environ["HF_TOKEN"],
    #     provider="novita",
    #     bill_to="huggingface"
    # )
    # subagent_model = InferenceClientModel(
    #     model_id=SUBAGENT_MODEL_ID,
    #     api_key=os.environ["HF_TOKEN"],
    #     provider="novita",
    #     bill_to="huggingface"
    # )

    # OpenAI via OpenAIServerModel (comentado):
    # coordinator_model = OpenAIServerModel(
    #     model_id=COORDINATOR_MODEL_ID,
    #     api_key=os.environ["OPENAI_API_KEY"],
    # )
    # subagent_model = OpenAIServerModel(
    #     model_id=SUBAGENT_MODEL_ID,
    #     api_key=os.environ["OPENAI_API_KEY"],
    # )

    # LiteLLM pointing at OpenAI (comentado):
    # coordinator_model = LiteLLMModel(
    #     model_id=f"openai/{COORDINATOR_MODEL_ID}",
    #     api_key=os.environ["OPENAI_API_KEY"],
    # )
    # subagent_model = LiteLLMModel(
    #     model_id=f"openai/{SUBAGENT_MODEL_ID}",
    #     api_key=os.environ["OPENAI_API_KEY"],
    # )

    # Vertex AI via LiteLLM (ativo) — usa VERTEXAI_PROJECT / VERTEXAI_LOCATION
    coordinator_model = LiteLLMModel(model_id=COORDINATOR_MODEL_ID)
    subagent_model = LiteLLMModel(model_id=SUBAGENT_MODEL_ID)

    tasks_by_id = {task.id: task for task in plan.subtasks}
    spawned_ids: list[str] = []
    followup_seq = 0

    def remaining_slots() -> int:
        return max(0, budget.subagents - len(spawned_ids))

    def reject(message: str) -> str:
        return json.dumps({"error": message, "remaining_slots": remaining_slots()}, ensure_ascii=False)

    with MCPClient(
        {"url": MCP_URL, "transport": "streamable-http"},
        structured_output=False,
    ) as mcp_tools:
        web_tools = wrap_local_firecrawl_tools(mcp_tools)

        def spawn_web(subtask: SubTask) -> str:
            if subtask.kind not in IMPLEMENTED_KINDS:
                return reject(
                    f"kind {subtask.kind.value} is not registered; only WEB can spawn"
                )
            print(f"Initializing Subagent for task {subtask.id} ({subtask.kind.value})...")
            worker = ToolCallingAgent(
                tools=web_tools,
                model=subagent_model,
                add_base_tools=False,
                name=worker_name(subtask.id),
                max_steps=subtask.max_calls + 1,
            )
            raw = worker.run(_subtask_prompt(plan, subtask))
            result = parse_subtask_result(raw, subtask.id)
            spawned_ids.append(subtask.id)
            return json.dumps(compact_result(result), ensure_ascii=False)

        @tool
        def run_subtask(subtask_id: str) -> str:
            """Run a planned WEB subtask and return compact SubTaskResult JSON.

            Args:
                subtask_id: id of a subtask from the clamped plan
            """
            if remaining_slots() <= 0:
                return reject("subagent budget exhausted")
            task = tasks_by_id.get(subtask_id)
            if task is None:
                return reject(f"unknown subtask_id {subtask_id}")
            if subtask_id in spawned_ids:
                return reject(f"subtask {subtask_id} already ran")
            return spawn_web(task)

        @tool
        def run_followup(objective: str, success_criteria: str) -> str:
            """Spawn an extra WEB subtask for a named gap after the first wave.

            Args:
                objective: one imperative sentence covering only the named gap
                success_criteria: what would count as done, including a declared gap
            """
            if remaining_slots() <= 0:
                return reject("subagent budget exhausted; cannot run follow-up")
            nonlocal followup_seq
            followup_seq += 1
            follow_id = f"followup_{followup_seq}"
            sisters = ", ".join(spawned_ids) or "planned wave"
            task = SubTask(
                id=follow_id,
                title=f"Follow-up {followup_seq}",
                objective=objective.strip(),
                output_format="SubTaskResult findings JSON",
                kind=AgentKind.WEB,
                sibling_boundaries=f"Do not repeat work already done by: {sisters}",
                success_criteria=success_criteria.strip(),
                max_calls=budget.calls_per_subagent,
            )
            tasks_by_id[follow_id] = task
            return spawn_web(task)

        coordinator = ToolCallingAgent(
            tools=[run_subtask, run_followup],
            model=coordinator_model,
            add_base_tools=False,
            name="coordinator_agent",
            max_steps=budget.subagents + 4,
        )

        coordinator_prompt = COORDINATOR_PROMPT_TEMPLATE.format(
            user_query=user_query,
            interpretation=plan.interpretation,
            scope=plan.scope,
            as_of=plan.as_of or "(now)",
            complexity=plan.complexity.value,
            max_subagents=budget.subagents,
            max_calls=budget.calls_per_subagent,
            domains_skipped=json.dumps(
                [item.model_dump() for item in plan.domains_skipped],
                ensure_ascii=False,
            ),
            subtasks_json=_planned_subtasks_json(plan),
        )

        final_report = coordinator.run(coordinator_prompt)
        if not isinstance(final_report, str):
            final_report = str(final_report)
        print(meter.format_one_line())
        return final_report.rstrip() + "\n" + meter.format_markdown()
