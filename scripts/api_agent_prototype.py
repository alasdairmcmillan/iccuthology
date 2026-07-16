"""Research prototype: is the manual Claude-Code/Antigravity + MCP tool-calling
workflow replicable by calling a provider's API directly instead of driving an
MCP-client IDE by hand?

Spawns the existing `phishpred-mcp` stdio server (unmodified) as a subprocess,
connects an MCP client to it, and drives an autonomous multi-turn tool-calling
loop against the SAME research tools a human-driven Claude Code/Antigravity
session would use (`song_history`, `venue_history`, `slot_propensities`,
`backtest_shortlist`, `run_context`, `heuristic_prediction`, `scoreboard`, ...),
finishing with a real `submit_prediction` call. Three providers, three
integration depths:

- anthropic: official SDK helper (`anthropic.lib.tools.mcp.async_mcp_tool` +
  `client.beta.messages.tool_runner`) -- least glue, closest match to what
  Claude Code does internally. Confirmed working live.
- google: `google-genai` *does* accept a raw MCP `ClientSession` directly as a
  tool (`GenerateContentConfig(tools=[session])`), which looks like the most
  automatic of the three -- but as tested (google-genai 2.11.0) it crashes,
  because `generate_content` deep-copies its config internally and a live
  `ClientSession` embeds asyncio internals that can't be deep-copied. Falls
  back to a manual loop with `FunctionDeclaration(parameters_json_schema=...)`
  (MCP's `inputSchema` passed straight through, no conversion needed). See
  `run_google`'s docstring. Confirmed working live via the fallback.
- openai: no built-in MCP client support (only remote-URL MCP on the Responses
  API, same shape/limitation as Anthropic's `mcp_servers` connector -- not
  applicable to a local stdio server). Requires a hand-rolled tool schema
  conversion (MCP `inputSchema` is already JSON Schema, so this is a near
  passthrough) and a manual `while` loop. Written and reviewed but NOT run
  live (no OPENAI_API_KEY configured at the time of this spike).

This is a THROWAWAY TEST -- submissions are written under an
`api-agent-test-<provider>` label to the real (gitignored)
data/predictions/submitted/ inbox via the server's own `submit_prediction`
tool, unmodified. Never `r2_push` these; delete the test label directories
once you've inspected them.

Usage:
    python scripts/api_agent_prototype.py --provider anthropic --show 2026-07-04
    python scripts/api_agent_prototype.py --provider openai
    python scripts/api_agent_prototype.py --provider google --model gemini-3.1-flash-lite

Requires the `agent-research` extra (`anthropic[mcp]`, `openai`,
`google-genai`) and the usual ANTHROPIC_API_KEY/OPENAI_API_KEY/GOOGLE_API_KEY
in `.env`/`.env.local`. Google's free tier is rate-limited tightly enough
(seen as low as 5 requests/minute, 20/day on some models) that a ~10-call
research loop can burn a day's quota on one show -- `gemini-2.5-flash` (this
project's other default) also 404s as "no longer available to new users" on
at least one tested key; `gemini-3.1-flash-lite` had headroom and is the
default here.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp import ClientSession, Tool
from mcp.client.stdio import StdioServerParameters, stdio_client

from phishpred import config

DEFAULT_MODELS = {
    "anthropic": "claude-opus-4-8",
    "openai": "gpt-4.1",
    "google": "gemini-3.1-flash-lite",  # gemini-2.5-flash 404s for new users on some keys
}

SYSTEM_PROMPT = (
    "You are an autonomous research agent for a Phish setlist prediction "
    "scoreboard. You have tools connected to the `phishpred` MCP server -- use "
    "them to research before predicting, exactly as a human-driven Claude "
    "Code/Antigravity session would."
)


def build_user_prompt(model_label: str, showdate: str) -> str:
    return f"""You are the model behind the "{model_label}" track on a Phish
setlist prediction scoreboard. THIS IS A RESEARCH-PROTOTYPE RUN -- "{model_label}"
is a throwaway test label, not a real competing track. Use the connected
`phishpred` MCP tools to research and then submit your prediction for
{showdate}.

Research first (read tools, any order):
- scoreboard(model_label="{model_label}") -- your track record (likely empty
  on a first test run).
- show_length_stats() -- songs-per-show averages; your shortlist's probs
  should sum to your EXPECTED HITS (~6-9 for a 30-song list), never the full
  show size.
- upcoming_shows() -- confirm the target show and note the epoch.
- run_context("{showdate}") -- the multi-night run; already-played nights
  matter.
- recent_setlists(10) -- current tour context.
- candidate_features("{showdate}") -- note played_in_run / played_prev_show
  flags.
- venue_history(...) and song_history(slug) for songs you want to check.
- heuristic_prediction("{showdate}") -- the statistical baseline. Beat it,
  don't copy it.
- slot_propensities([...]) -- where each song tends to sit, before your
  setlist call.
- backtest_shortlist([...]) -- test your working hypothesis before
  submitting.

Hard rules:
- played_in_run=1 means ~0-5% probability tonight (within-run repeats are
  essentially unheard of).
- played_prev_show=1 (played the immediately preceding show) repeats only
  ~2% of the time.

Then call submit_prediction exactly once for {showdate}:
- model_label="{model_label}" (exactly this string)
- predictions: 20-40 {{slug, prob}} objects, prob in (0,1], honest per-song
  probabilities summing to your expected hits (~6-9), NOT the full show
  size.
- setlist: {{"sets": {{"1": [...], "2": [...], "e": [...]}}}} -- your full
  ordered setlist call, opener/closer conscious, no slug repeated.
- rationale: 2-5 sentences specific to this show and your research.

This is a test run: do NOT run scripts/r2_push.py or otherwise publish this
submission -- it stays local under the "{model_label}" test label only."""


@asynccontextmanager
async def connect_mcp():
    """Spawn the existing phishpred-mcp stdio server and connect a client
    session to it -- the same server a human's MCP-client IDE would attach
    to, unmodified."""
    params = StdioServerParameters(command=sys.executable, args=["-m", "phishpred.mcp.server"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            yield session, tools


def _mcp_result_text(result: Any) -> str:
    parts = []
    for block in result.content:
        text = getattr(block, "text", None)
        parts.append(text if text is not None else repr(block))
    joined = "\n".join(parts)
    if getattr(result, "isError", False):
        return f"ERROR: {joined}"
    return joined


async def run_anthropic(
    session: ClientSession, tools: list[Tool], showdate: str, label: str, model: str
) -> tuple[list[str], Any]:
    from anthropic import AsyncAnthropic
    from anthropic.lib.tools.mcp import async_mcp_tool

    client = AsyncAnthropic()
    anthropic_tools = [async_mcp_tool(t, session) for t in tools]
    runner = client.beta.messages.tool_runner(
        model=model,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        tools=anthropic_tools,
        messages=[{"role": "user", "content": build_user_prompt(label, showdate)}],
    )
    call_log: list[str] = []
    final = None
    async for message in runner:
        final = message
        for block in message.content:
            if block.type == "tool_use":
                call_log.append(block.name)
    return call_log, final


async def run_openai(
    session: ClientSession, tools: list[Tool], showdate: str, label: str, model: str
) -> tuple[list[str], Any]:
    from openai import AsyncOpenAI

    client = AsyncOpenAI()
    oa_tools = [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description or "",
                "parameters": t.inputSchema,
            },
        }
        for t in tools
    ]
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_prompt(label, showdate)},
    ]
    call_log: list[str] = []
    for _ in range(30):
        resp = await client.chat.completions.create(
            model=model, messages=messages, tools=oa_tools, max_tokens=4096,
        )
        msg = resp.choices[0].message
        messages.append(msg.model_dump(exclude_none=True))
        if not msg.tool_calls:
            return call_log, resp
        for tc in msg.tool_calls:
            call_log.append(tc.function.name)
            args = json.loads(tc.function.arguments or "{}")
            result = await session.call_tool(tc.function.name, args)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": _mcp_result_text(result),
            })
    raise RuntimeError("openai tool-calling loop did not converge in 30 turns")


async def run_google(
    session: ClientSession, tools: list[Tool], showdate: str, label: str, model: str
) -> tuple[list[str], Any]:
    """NOTE: google-genai's `tools=[session]` (pass the raw MCP ClientSession
    directly, letting the SDK list/call tools itself) is documented and looks
    like the most automatic of the three integrations -- but as tested against
    google-genai 2.11.0 it crashes: `generate_content` deep-copies its
    `GenerateContentConfig` internally, and a live `ClientSession` embeds
    asyncio internals (`TypeError: cannot pickle '_asyncio.Future' object`).
    Falling back to the same manual loop shape as OpenAI: MCP `inputSchema` is
    passed through untouched via `FunctionDeclaration.parameters_json_schema`
    (no manual schema conversion needed), and we drive the turns ourselves.
    """
    from google import genai
    from google.genai import types

    client = genai.Client()
    declarations = [
        types.FunctionDeclaration(
            name=t.name, description=t.description or "", parameters_json_schema=t.inputSchema,
        )
        for t in tools
    ]
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        tools=[types.Tool(function_declarations=declarations)],
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )
    contents: list[types.Content] = [
        types.Content(role="user", parts=[types.Part(text=build_user_prompt(label, showdate))])
    ]
    call_log: list[str] = []
    response = None
    for _ in range(30):
        response = await client.aio.models.generate_content(model=model, contents=contents, config=config)
        candidate = response.candidates[0]
        contents.append(candidate.content)
        function_calls = [p.function_call for p in candidate.content.parts if p.function_call is not None]
        if not function_calls:
            return call_log, response
        response_parts = []
        for fc in function_calls:
            call_log.append(fc.name)
            result = await session.call_tool(fc.name, dict(fc.args or {}))
            response_parts.append(
                types.Part.from_function_response(
                    name=fc.name, response={"result": _mcp_result_text(result)}
                )
            )
        contents.append(types.Content(role="user", parts=response_parts))
    raise RuntimeError("google tool-calling loop did not converge in 30 turns")


RUNNERS = {"anthropic": run_anthropic, "openai": run_openai, "google": run_google}


async def _amain(args: argparse.Namespace) -> None:
    config._load_env()  # loads .env then .env.local (override=True), same as models/llm.py
    model = args.model or DEFAULT_MODELS[args.provider]
    label = args.label or f"api-agent-test-{args.provider}"

    async with connect_mcp() as (session, tools):
        print(f"Connected to phishpred-mcp: {len(tools)} tools available")

        showdate = args.show
        if not showdate:
            result = await session.call_tool("upcoming_shows", {"limit": 1})
            payload = json.loads(_mcp_result_text(result))
            showdate = payload["shows"][0]["showdate"]
            print(f"No --show given; using next upcoming show: {showdate}")

        runner = RUNNERS[args.provider]
        call_log, final = await runner(session, tools, showdate, label, model)

    print(f"\n=== {args.provider} ({model}) -- {len(call_log)} tool call(s) ===")
    for i, name in enumerate(call_log, 1):
        print(f"  {i}. {name}")

    submitted_path = Path("data/predictions/submitted") / label / f"{showdate}.json"
    if submitted_path.exists():
        payload = json.loads(submitted_path.read_text(encoding="utf-8"))
        n_preds = len(payload.get("predictions", []))
        has_setlist = "sets" in (payload.get("setlist") or {})
        print(f"\nWrote {submitted_path} ({n_preds} predictions, setlist={has_setlist})")
    else:
        print(f"\nNo submission found at {submitted_path} -- submit_prediction was not called.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", required=True, choices=sorted(RUNNERS))
    parser.add_argument("--show", help="Showdate YYYY-MM-DD; defaults to the next upcoming show")
    parser.add_argument("--model", help="Override the default model id for the provider")
    parser.add_argument("--label", help="Override the model_label (default: api-agent-test-<provider>)")
    args = parser.parse_args()
    asyncio.run(_amain(args))


if __name__ == "__main__":
    main()
