# Firecrawl + Smolagents Deep Research (Multi‑Agent)

Deep‑research system that takes a user query, clarifies it (Ask), maps candidate sources (Describe), plans the work as a typed `ResearchPlan`, and orchestrates WEB sub‑agents under a complexity budget. A coordinator synthesizes compact `Finding` JSON into a single markdown report.

## Links
- [YouTube video tutorial](https://www.youtube.com/watch?v=vHBRmXpDIFY)
- [Written version of the tutorial](https://alejandro-ao.com/posts/agents/multi-agent-deep-research/)

## Highlights
- Built on `smolagents` (by Hugging Face) for agent orchestration and tool calling.
- LLM calls run via **Vertex AI (LiteLLM)** by default. OpenAI and Hugging Face blocks stay commented in place — switch by commenting/uncommenting.
- Uses a **local** Firecrawl MCP (self-hosted API + `firecrawl-mcp`) for web research and retrieval.
- `mcp` is pinned to 1.x (`<2`) because smolagents' MCP client still imports `streamablehttp_client`.
- Sprint 5: Describe step maps dozens–100+ candidate URLs (multi-query recon, deduped, scaled by complexity) between Ask and Plan; planner receives `DomainProfile` + `mapped_sources`.
- Sprint 3: Ask step (`clarifier.py`) agrees interpretation, scope, `as_of`, angles, and complexity before planning. CLI always shows the draft and waits for confirm or free-text add/remove; evals skip the dialogue and pin `as_of` from the YAML.
- Sprint 2: typed contracts, budget by complexity, `as_of`, subagents do not receive the full plan and return `Finding` JSON (not markdown).
- Sprint 1 eval harness in `evals/`: 20 queries, LLM-as-judge, baseline runner.

## How It Works
- Ask: `clarifier.py` emits a `Clarification` JSON (`vertex_ai/gemini-2.5-pro`). CLI is interactive: it always prints the draft (interpretation, scope, `as_of`, complexity, angles) and the user confirms (empty Enter / `ok`) or types what to add, remove, or correct, in a loop (cap 5 revisions) before Describe runs. Evals pass `interactive=False` so they never block on stdin; if the query has `as_of`, the harness pins it.
- Describe: `sources.py` (`DomainRegistry.describe_all`) runs breadth-first Firecrawl `/v2/search` recon from the clarification (short queries from interpretation + angles). Budgets by complexity (lookup→25 URLs … deep→120). Dedupes by URL, fills `mapped_sources` / `dominant_hosts`. Does **not** scrape; subagents do that later.
- Plan: `planner.py` splits the agreed clarification **and** truncated domain profiles into a `ResearchPlan` JSON (`vertex_ai/gemini-2.5-pro`). The harness `apply_clarification`s then `clamp_plan`s (Ask fields win, then subagent cap, `kind=WEB`, `max_calls`).
- Coordinator: `coordinator.py` (`vertex_ai/gemini-2.5-pro`) runs planned subtasks, may add one follow-up wave for named gaps if budget remains, then synthesizes.
- Sub-agents (`vertex_ai/gemini-2.5-flash`, `kind=WEB` only): receive the `SubTask` + interpretation + `as_of`, not the whole plan. `max_steps` is the call cap. They return `SubTaskResult` JSON.
- Splitter (`task_splitter.py`) is retired.

![Open Deep Research Workflow Diagram](docs/open-deep-research-workflow-diagram.png)

## Models & Providers

Active runtime is **Vertex AI via LiteLLM** (GCP project `deepdocs-494218`, location `us-central1` by default). Harder steps use Pro; high-volume search uses Flash.

| Role | Active (Vertex) | OpenAI (commented) | Hugging Face (commented) |
|---|---|---|---|
| Clarifier | `vertex_ai/gemini-2.5-pro` | `gpt-4.1` | `moonshotai/Kimi-K2-Thinking` |
| Planner | `vertex_ai/gemini-2.5-pro` | `gpt-4.1` | `moonshotai/Kimi-K2-Thinking` |
| Coordinator | `vertex_ai/gemini-2.5-pro` | `gpt-4.1` | `MiniMaxAI/MiniMax-M1-80k` |
| Sub-agents | `vertex_ai/gemini-2.5-flash` | `gpt-4o` | `MiniMaxAI/MiniMax-M1-80k` |
| Eval judge | `vertex_ai/gemini-2.5-pro` | `gpt-4.1` | — |

JSON chat for Ask/Plan/judge goes through `llm_clients.chat_json`. Coordinator/subagents use smolagents `LiteLLMModel`.

**Auth (required for Vertex):**

```powershell
gcloud auth application-default login
gcloud auth application-default set-quota-project deepdocs-494218
```

List publisher Gemini models:

```powershell
gcloud beta ai model-garden models list --model-filter=gemini --project=deepdocs-494218
```

**Switch back to OpenAI:** in `llm_clients.py`, `clarifier.py`, `planner.py`, `evals/judge.py`, and `coordinator.py`, comment the Vertex blocks and uncomment the OpenAI blocks (and set `OPENAI_API_KEY`). Same idea as the old HF toggles.

**Switch to Hugging Face:** uncomment the `InferenceClient` / `InferenceClientModel` blocks and restore the HF `MODEL_ID` constants.

Budget (enforced in `contracts.BUDGETS`, not only in the prompt):

| Complexity | Subagents | Calls / subagent |
|---|---|---|
| lookup | 1 | 5 |
| comparison | 4 | 12 |
| exploratory | 6 | 15 |
| deep | 12 | 20 |

## Firecrawl MCP Tools
- Configured in `coordinator.py` and given only to WEB workers via `MCPClient`.
- Workers talk to `http://localhost:3000/mcp` (local `firecrawl-mcp`), which forwards to the self-hosted API at `http://localhost:3002`.
- Workers do **not** get the raw MCP schemas. `local_firecrawl_tools.py` wraps only `firecrawl_scrape` and `firecrawl_search` with a self-host-safe surface: scrape accepts `url` + optional `formats` (forces `proxy=basic`, `onlyMainContent=true`); search accepts `query` + optional `limit`. Cloud-only fields (`queryOptions`, `proxy: auto`, extract, etc.) never reach the Docker API.
- The coordinator has no search tools.
- No Firecrawl cloud key. Order: Docker API → MCP → agents.

## Setup
- Requirements: Python `3.11` (`.python-version`), Docker Desktop, Node.js 22+, internet access, **gcloud ADC** for Vertex (or `OPENAI_API_KEY` if you switch providers).
- Recommended (uv):
  - `uv sync` to create `.venv` and install deps from `pyproject.toml`
  - For editable install: `uv pip install -e .`
- Fallback (pip): `pip install -e .`

## Configuration
- Environment variables in this repo (`.env` or your shell):
  - `VERTEXAI_PROJECT` / `VERTEX_PROJECT`: GCP project (default `deepdocs-494218`).
  - `VERTEXAI_LOCATION` / `VERTEX_LOCATION`: Vertex region (default `us-central1`).
  - `FIRECRAWL_MCP_URL`: local MCP endpoint (default `http://localhost:3000/mcp`).
  - `OPENAI_API_KEY`: only needed if you uncomment the OpenAI path.
  - `HF_TOKEN`: only needed if you uncomment the Hugging Face path.
- Model selection: edit the `MODEL_ID` / `COORDINATOR_MODEL_ID` / `SUBAGENT_MODEL_ID` constants (LiteLLM ids use the `vertex_ai/` prefix).

## 1. Self-host Firecrawl (Docker)

The API is a separate clone (sibling folder `../firecrawl`). Pin release `v2.11.162` as in the [official self-host guide](https://docs.firecrawl.dev/contributing/self-host).

```
git clone https://github.com/firecrawl/firecrawl.git
cd firecrawl
git checkout v2.11.162
```

Create `firecrawl/.env` (this is **not** this project's `.env`). Use a random Postgres password, not pgAdmin's. Keep `POSTGRES_DB=postgres`.

```
USE_DB_AUTHENTICATION=false
POSTGRES_USER=postgres
POSTGRES_PASSWORD=replace-with-at-least-32-random-characters
POSTGRES_DB=postgres
```

Do not copy `apps/api/.env.example` into Compose. Then:

```
docker compose up --build -d
docker compose ps --all
```

Give the stack a minute. Check the API (Command Prompt, one line):

```
curl.exe --fail --silent --show-error --max-time 5 http://localhost:3002/v0/health/readiness
```

Expected: `{"status":"ok"}`. Then a real scrape:

```
curl.exe --fail-with-body --silent --show-error --max-time 75 -X POST http://localhost:3002/v2/scrape -H "Content-Type: application/json" -d "{""url"":""https://example.com"",""formats"":[""markdown""],""timeout"":60000}"
```

`"success": true` means the API is usable. Leave Compose running. Do not expose port `3002` to the internet: this baseline has auth off.

## 2. Local MCP

The Docker stack is the API only. Agents need `firecrawl-mcp` on the host (port `3000`), pointing at `:3002`. Node 22+:

```
node --version
```

Leave this process running in its own terminal. Command Prompt:

```
set FIRECRAWL_API_URL=http://localhost:3002
set HTTP_STREAMABLE_SERVER=true
npx -y firecrawl-mcp@3.23.7
```

PowerShell:

```
$env:FIRECRAWL_API_URL = "http://localhost:3002"
$env:HTTP_STREAMABLE_SERVER = "true"
npx -y firecrawl-mcp@3.23.7
```

Do **not** set `FIRECRAWL_API_KEY`. Without `FIRECRAWL_API_URL`, the MCP talks to Firecrawl Cloud.

Check (another terminal):

```
curl.exe --fail --silent --show-error http://localhost:3000/health
```

Expected: `ok`. The client URL is `http://localhost:3000/mcp` (not `/v2/mcp`). That value is already in this project's `.env` as `FIRECRAWL_MCP_URL`.

## Run

With Docker on `:3002` and MCP on `:3000`:

- `uv run main.py` or `.venv/Scripts/python.exe main.py`
- Enter your query when prompted. The clarifier always shows a draft research plan. Empty Enter or `ok` confirms it; any other text adds, removes, or corrects (loop until confirm, cap 5). Then it prints the agreed scope and runs. The final consolidated report is written to `research_result.md` and ends with a **Uso de tokens (LLM)** section (Ask/Plan/coordinator/subagents only; Describe/Firecrawl excluded).

## Evals

Cheap default: one WEB lookup (`q11`, salário mínimo). That is the case that fits the current system (web only, likely `lookup` budget).

```
python evals/run_smoke.py
python evals/run_baseline.py --smoke
```

The full 20 queries in `evals/queries.yaml` are opt-in and expensive. Do not run them casually.

```
python evals/run_baseline.py --ids q11,q14
python evals/run_baseline.py
```

Each query’s research token usage is printed (`tokens_total=…`), stored under `results[].tokens` in `scores.json`, and summarized as `tokens_by_id` / `tokens_total` / `tokens_mean` (judge tokens are excluded).

Raw reports under `evals/baselines/` are gitignored; `queries.yaml` and the harness are versioned.

## File Map
- `main.py`: CLI entry point (`interactive=True`).
- `clarifier.py`: Ask step; `Clarification` JSON; CLI review loop before Describe (Vertex via `llm_clients`; OpenAI/HF commented).
- `sources.py`: `WebDomain` describe/search/fetch, `DomainRegistry`, multi-query source map (Sprint 5).
- `contracts.py`: `Clarification`, `DomainProfile`, `ResearchPlan`, `SubTask`, `Finding`, budgets, `DESCRIBE_BUDGETS`, `pin_as_of`, `apply_clarification`, `clamp_plan`.
- `llm_clients.py`: `chat_json` — Vertex/LiteLLM active; OpenAI block commented for easy revert.
- `token_meter.py`: LiteLLM success callback; totals appended to the report and captured per eval query.
- `planner.py`: typed plan from clarification + domain profiles (Vertex; OpenAI/HF commented).
- `coordinator.py`: Ask → Describe → Plan → factory `kind=WEB`, budget caps, compact `SubTaskResult` back to the lead (`LiteLLMModel` Vertex).
- `local_firecrawl_tools.py`: self-host-safe scrape+search wrappers over Firecrawl MCP.
- `prompts.py`: clarifier, planner, coordinator, sub-agent (no tutorial “insecurity” outline).
- `task_splitter.py`: deprecated stub.
- `tests/test_sources.py`: mocked Describe/map unit tests.
- `tests/test_token_meter.py`: token meter unit tests.
- `evals/`: queries, judge, baseline runner, `run_smoke.py` (q11 only). Evals call Ask with `interactive=False`; `scores.json` includes per-query tokens.

## Notes
- Only `AgentKind.WEB` can spawn. Other kinds exist on the enum and are listed in `domains_skipped` until later sprints.
- Parallel wave spawn, on-disk artifacts, and CitationAgent are sprint 6.
