import json

# from openai import OpenAI

from contracts import (
    RESEARCH_PLAN_JSON_SCHEMA,
    Clarification,
    DomainProfile,
    ResearchPlan,
    apply_clarification,
    clamp_plan,
)
from llm_clients import chat_json
from prompts import PLANNER_SYSTEM_INSTRUCTIONS
from sources import profiles_for_planner

# Hugging Face Inference (comentado — reative se os créditos voltarem)
# from huggingface_hub import InferenceClient
# import os


def generate_research_plan(
    clarification: Clarification,
    profiles: list[DomainProfile] | None = None,
) -> ResearchPlan:
    # MODEL_ID = "moonshotai/Kimi-K2-Thinking"
    # PROVIDER = "auto"

    # OpenAI (comentado — descomente e comente o bloco Vertex para voltar)
    # MODEL_ID = "gpt-4.1"
    # PROVIDER = "openai"

    # Vertex AI via LiteLLM (ativo)
    MODEL_ID = "vertex_ai/gemini-2.5-pro"
    PROVIDER = "vertex"

    print("Generating the research plan from the agreed clarification")
    print("MODEL: ", MODEL_ID)
    print("PROVIDER: ", PROVIDER)

    domain_profiles = profiles_for_planner(profiles or [])
    user_payload = {
        "clarification": clarification.model_dump(exclude={"questions", "answers"}),
        "domain_profiles": domain_profiles,
    }
    user_content = json.dumps(user_payload, indent=2, ensure_ascii=False)

    # planner_client = InferenceClient(
    #     api_key=os.environ["HF_TOKEN"],
    #     bill_to="huggingface",
    #     provider=PROVIDER,
    # )

    # OpenAI (comentado):
    # planner_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    # completion = planner_client.chat.completions.create(
    #     model=MODEL_ID,
    #     messages=[
    #         {"role": "system", "content": PLANNER_SYSTEM_INSTRUCTIONS.strip()},
    #         {"role": "user", "content": user_content},
    #     ],
    #     response_format={
    #         "type": "json_schema",
    #         "json_schema": RESEARCH_PLAN_JSON_SCHEMA,
    #     },
    # )
    # payload = json.loads(completion.choices[0].message.content)

    payload = chat_json(
        MODEL_ID,
        [
            {"role": "system", "content": PLANNER_SYSTEM_INSTRUCTIONS.strip()},
            {"role": "user", "content": user_content},
        ],
        RESEARCH_PLAN_JSON_SCHEMA,
    )
    plan = ResearchPlan.model_validate(payload)
    plan = apply_clarification(plan, clarification)
    plan = clamp_plan(plan)

    print("\033[93mGenerated Research Plan:\033[0m")
    print(f"  complexity={plan.complexity.value} as_of={plan.as_of or '(now)'}")
    print(f"  interpretation={plan.interpretation[:240]}")
    for task in plan.subtasks:
        print(f"\033[93m  [{task.id}] {task.title} ({task.kind.value}, max_calls={task.max_calls})\033[0m")
        print(f"    {task.objective}")
    if plan.domains_skipped:
        print("  skipped:")
        for skip in plan.domains_skipped:
            print(f"    - {skip.kind.value}: {skip.reason}")
    print()
    return plan
