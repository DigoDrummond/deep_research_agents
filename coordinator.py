from stdio_utf8 import configure_utf8_stdio

configure_utf8_stdio()

from citations import apply_citations
from clarifier import clarify
from planner import generate_research_plan
from prompts import SUBAGENT_PROMPT_TEMPLATE, COORDINATOR_PROMPT_TEMPLATE
from smolagents import LiteLLMModel, ToolCallingAgent, MCPClient, tool, InferenceClientModel, OpenAIServerModel
import concurrent.futures
import os
import json
from datetime import datetime, timezone
from typing import Any

import memory

from contracts import (
    IMPLEMENTED_KINDS,
    AgentKind,
    ResearchPlan,
    SubTask,
    SubTaskResult,
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
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

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
    memory.save_plan(run_id, plan)

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
    all_results: list[SubTaskResult] = []
    all_documents: list[dict[str, Any]] = []

    def remaining_slots() -> int:
        return max(0, budget.subagents - len(spawned_ids))

    def reject(message: str) -> dict[str, Any]:
        return {"error": message}

    with MCPClient(
        {"url": MCP_URL, "transport": "streamable-http"},
        structured_output=False,
    ) as mcp_tools:
        web_tools = wrap_local_firecrawl_tools(mcp_tools)

        def _run_single(subtask: SubTask) -> tuple[SubTaskResult, list[dict[str, Any]]]:
            """Runs one worker to completion. Called from a pool thread."""
            print(f"Initializing Subagent for task {subtask.id} ({subtask.kind.value})...")
            worker = ToolCallingAgent(
                tools=web_tools,
                model=subagent_model,
                add_base_tools=False,
                name=worker_name(subtask.id),
                max_steps=subtask.max_calls + 1,
            )
            try:
                raw = worker.run(_subtask_prompt(plan, subtask))
                result = parse_subtask_result(raw, subtask.id)
            except Exception as exc:  # a crashed worker still owes the wave a result
                result = SubTaskResult(
                    subtask_id=subtask.id,
                    findings=[],
                    gaps=[f"Worker raised an exception: {exc}"],
                    calls_used=0,
                )
            # Best-effort even on a crashed worker: memory.steps still holds
            # whatever the worker fetched before it failed.
            documents = memory.extract_documents(worker.memory.steps)
            return result, documents

        def _dispatch_wave(tasks: list[SubTask]) -> dict[str, dict[str, Any]]:
            """Harness-level fan-out: run every task in `tasks` concurrently and
            wait for all of them. Slot accounting happens up front, once, so two
            tasks in the same wave cannot race for the last remaining slot.
            Every SubTaskResult (compact or not) is kept in `all_results`, and
            every fetched document in `all_documents`, so citations.py can
            anchor the final report against everything collected across every
            wave, not just the last one. Each result is also persisted to disk
            (memory.py, sprint 6.3) with a real ArtifactRef.path attached.
            """
            outcomes: dict[str, dict[str, Any]] = {}
            runnable: list[SubTask] = []
            for task in tasks:
                if task.kind not in IMPLEMENTED_KINDS:
                    outcomes[task.id] = reject(
                        f"kind {task.kind.value} is not registered; only WEB can spawn"
                    )
                elif task.id in spawned_ids:
                    outcomes[task.id] = reject(f"subtask {task.id} already ran")
                else:
                    runnable.append(task)

            slots = remaining_slots()
            if len(runnable) > slots:
                for task in runnable[slots:]:
                    outcomes[task.id] = reject("subagent budget exhausted")
                runnable = runnable[:slots]

            if runnable:
                max_workers = max(1, min(budget.parallelism, len(runnable)))
                print(
                    f"Dispatching wave of {len(runnable)} subagent(s), "
                    f"parallelism={max_workers}..."
                )
                with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
                    future_to_task = {pool.submit(_run_single, task): task for task in runnable}
                    for future in concurrent.futures.as_completed(future_to_task):
                        task = future_to_task[future]
                        result, documents = future.result()
                        memory.save_documents(run_id, task.id, documents)
                        all_documents.extend(documents)
                        artifact = memory.save_subtask_result(run_id, result)
                        result = result.model_copy(update={"artifacts": [artifact]})
                        all_results.append(result)
                        outcomes[task.id] = compact_result(result)
                # Only mark tasks spawned once the pool has fully drained, so
                # remaining_slots() never reflects an in-flight, unfinished task.
                spawned_ids.extend(task.id for task in runnable)
            return outcomes

        @tool
        def run_wave(subtask_ids: list[str]) -> str:
            """Run a wave of planned WEB subtasks in parallel and return their results.

            Call this ONCE with the whole set of subtask ids you want to run
            together (e.g. the entire planned wave). The harness dispatches all
            of them concurrently and only returns once every one has finished.

            Args:
                subtask_ids: ids of subtasks from the clamped plan to run in this wave
            """
            tasks: list[SubTask] = []
            outcomes: dict[str, dict[str, Any]] = {}
            for subtask_id in subtask_ids:
                task = tasks_by_id.get(subtask_id)
                if task is None:
                    outcomes[subtask_id] = reject(f"unknown subtask_id {subtask_id}")
                else:
                    tasks.append(task)
            outcomes.update(_dispatch_wave(tasks))
            ordered = {sid: outcomes[sid] for sid in subtask_ids if sid in outcomes}
            return json.dumps(
                {"results": ordered, "remaining_slots": remaining_slots()},
                ensure_ascii=False,
            )

        @tool
        def run_followup_wave(followups: list[dict]) -> str:
            """Spawn a batch of follow-up WEB subtasks for named gaps, in parallel.

            Call this AT MOST ONCE, after reading run_wave's results, and only
            for gaps you can name. Each item becomes one new subagent; all of
            them run concurrently. The harness rejects items once the subagent
            budget is exhausted.

            Args:
                followups: list of objects, each with "objective" (one imperative
                    sentence covering only the named gap) and "success_criteria"
                    (what would count as done, including a declared gap)
            """
            nonlocal followup_seq
            tasks: list[SubTask] = []
            ids_in_order: list[str] = []
            for item in followups:
                followup_seq += 1
                follow_id = f"followup_{followup_seq}"
                sisters = ", ".join(spawned_ids) or "planned wave"
                task = SubTask(
                    id=follow_id,
                    title=f"Follow-up {followup_seq}",
                    objective=str(item.get("objective", "")).strip(),
                    output_format="SubTaskResult findings JSON",
                    kind=AgentKind.WEB,
                    sibling_boundaries=f"Do not repeat work already done by: {sisters}",
                    success_criteria=str(item.get("success_criteria", "")).strip(),
                    max_calls=budget.calls_per_subagent,
                )
                tasks_by_id[follow_id] = task
                tasks.append(task)
                ids_in_order.append(follow_id)
            outcomes = _dispatch_wave(tasks)
            ordered = {sid: outcomes[sid] for sid in ids_in_order if sid in outcomes}
            return json.dumps(
                {"results": ordered, "remaining_slots": remaining_slots()},
                ensure_ascii=False,
            )

        coordinator = ToolCallingAgent(
            tools=[run_wave, run_followup_wave],
            model=coordinator_model,
            add_base_tools=False,
            name="coordinator_agent",
            max_steps=8,
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

        all_findings = [finding for result in all_results for finding in result.findings]
        final_report, _citations = apply_citations(final_report, all_findings, all_documents)

        print(meter.format_one_line())
        return final_report.rstrip() + "\n" + meter.format_markdown()
