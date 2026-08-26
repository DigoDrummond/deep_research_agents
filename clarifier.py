from collections.abc import Callable
import json

# from openai import OpenAI

from contracts import CLARIFICATION_JSON_SCHEMA, Clarification, pin_as_of
from llm_clients import chat_json
from prompts import CLARIFIER_SYSTEM_INSTRUCTIONS

# Hugging Face Inference (comentado — reative se os créditos voltarem)
# from huggingface_hub import InferenceClient
# MODEL_ID = "moonshotai/Kimi-K2-Thinking"
# PROVIDER = "auto"

# OpenAI (comentado — descomente e comente o bloco Vertex para voltar)
# MODEL_ID = "gpt-4.1"
# PROVIDER = "openai"

# Vertex AI via LiteLLM (ativo)
MODEL_ID = "vertex_ai/gemini-2.5-pro"
PROVIDER = "vertex"

CONFIRM_REPLIES = frozenset({"", "ok", "yes", "sim"})
MAX_REVISIONS = 5


def _is_confirm(text: str | None) -> bool:
    if text is None:
        return True
    return text.strip().lower() in CONFIRM_REPLIES


def review_stdin(_clarification: Clarification) -> str | None:
    print("Empty Enter or 'ok' confirms. Anything else adds, removes, or corrects.")
    text = input("     > ").strip()
    print()
    if _is_confirm(text):
        return None
    return text


def _print_clarification(clarification: Clarification, *, label: str) -> None:
    print(f"\033[93m{label}:\033[0m")
    print(f"  complexity={clarification.complexity.value} as_of={clarification.as_of or '(now)'}")
    print(f"  interpretation={clarification.interpretation}")
    print(f"  scope={clarification.scope}")
    if clarification.angles_considered:
        print("  angles:")
        for angle in clarification.angles_considered:
            print(f"    - {angle}")
    if clarification.questions:
        print("  open questions:")
        for question in clarification.questions:
            print(f"    - {question}")
    if clarification.answers:
        print("  revisions recorded:", len(clarification.answers))
    print()


def _complete(messages: list[dict[str, str]]) -> Clarification:
    # clarifier_client = InferenceClient(
    #     api_key=os.environ["HF_TOKEN"],
    #     bill_to="huggingface",
    #     provider=PROVIDER,
    # )
    # completion = clarifier_client.chat.completions.create(...)

    # OpenAI (comentado):
    # clarifier_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    # completion = clarifier_client.chat.completions.create(
    #     model=MODEL_ID,
    #     messages=messages,
    #     response_format={
    #         "type": "json_schema",
    #         "json_schema": CLARIFICATION_JSON_SCHEMA,
    #     },
    # )
    # payload = json.loads(completion.choices[0].message.content)

    payload = chat_json(MODEL_ID, messages, CLARIFICATION_JSON_SCHEMA)
    questions = payload.get("questions")
    if isinstance(questions, list):
        payload["questions"] = questions[:4]
    return Clarification.model_validate(payload)


def _user_message(user_query: str, as_of: str | None) -> str:
    if as_of:
        return (
            f"{user_query}\n\n"
            f"Harness as_of (mandatory evidence cutoff, YYYY-MM-DD): {as_of}. "
            "You must set as_of to this date."
        )
    return user_query


def _revise_message(
    user_query: str,
    draft: Clarification,
    feedback: str,
    as_of: str | None,
) -> str:
    cutoff = ""
    if as_of:
        cutoff = f"\nHarness as_of (mandatory): {as_of}\n"
    current = draft.model_dump(exclude={"answers"}, exclude_none=True)
    return (
        f"Original query:\n{user_query}\n"
        f"{cutoff}\n"
        f"Current draft clarification:\n{json.dumps(current, indent=2, ensure_ascii=False)}\n\n"
        f"User revision request:\n{feedback}\n\n"
        "This is a revise turn. Apply the request exactly: add, remove, or correct "
        "interpretation, scope, angles, complexity, or as_of as asked. "
        "Do not reopen what the user did not touch. questions MUST be []."
    )


def clarify(
    user_query: str,
    *,
    interactive: bool = False,
    as_of: str | None = None,
    review: Callable[[Clarification], str | None] | None = None,
) -> Clarification:
    print("Clarifying the research query...")
    print("MODEL: ", MODEL_ID)
    print("PROVIDER: ", PROVIDER)

    draft = _complete(
        [
            {"role": "system", "content": CLARIFIER_SYSTEM_INSTRUCTIONS.strip()},
            {"role": "user", "content": _user_message(user_query, as_of)},
        ]
    )
    draft = pin_as_of(draft, as_of)
    draft = draft.model_copy(update={"original_query": user_query})

    if not interactive:
        result = draft.model_copy(update={"questions": [], "answers": []})
        _print_clarification(result, label="Agreed clarification")
        return result

    callback = review if review is not None else review_stdin
    revisions: list[str] = []

    while True:
        _print_clarification(draft, label="Draft research plan (not yet agreed)")
        feedback = (callback(draft) or "").strip()
        if _is_confirm(feedback):
            break
        if len(revisions) >= MAX_REVISIONS:
            print(f"Revision cap ({MAX_REVISIONS}) reached; proceeding with the last draft.\n")
            break
        revisions.append(feedback)
        draft = _complete(
            [
                {"role": "system", "content": CLARIFIER_SYSTEM_INSTRUCTIONS.strip()},
                {"role": "user", "content": _revise_message(user_query, draft, feedback, as_of)},
            ]
        )
        draft = pin_as_of(draft, as_of)
        draft = draft.model_copy(update={"original_query": user_query})

    final = draft.model_copy(update={"questions": [], "answers": revisions})
    _print_clarification(final, label="Agreed clarification")
    return final
