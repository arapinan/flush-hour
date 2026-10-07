"""Flush Hour: the web server, the agent harness (tool-calling loop), and the session store."""

import json
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import litellm
import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel

from tools import TOOLS, run_tool

# --- Config ---

NYC_TZ = ZoneInfo("America/New_York")
MAX_TOOL_ROUNDS = 6  # the harness, not the model, bounds how long one turn can run
RATE_LIMIT_RETRIES = 2  # extra attempts after a 429 from the model API
RATE_LIMIT_BACKOFF_S = 2  # wait 2 s, then 4 s

# The prompt says when (and when not) to call each tool; the tool schemas in tools.py
# describe the tools themselves.

SYSTEM_PROMPT = """You are Flush Hour, a dry, quick-witted New Yorker who knows where every public restroom in the five boroughs is, and which ones are actually open. People come to you in a hurry, so be brief and practical.

You have tools backed by real city data. NEVER name a restroom from memory; every restroom you mention must come from a tool result.

Which tool, when:
- "Nearest / where can I find a restroom": find_restrooms.
- The user is walking from A to B: restrooms_along_route. Prefer it over find_restrooms whenever two places are mentioned.
- Follow-up about one restroom you already listed ("will that be open at 9?"): check_open_status with its id.
- find_restrooms returned no results, or the user wants other options: fallback_options. When find_restrooms comes back empty, call fallback_options in the same turn instead of asking first, then answer using both.
- The user states a standing need (wheelchair access, a baby changing station, all-gender restroom): call remember_needs once so later searches apply it. Do not re-ask.

Rules:
- Pass the user's place name as they wrote it, except expand a well-known nickname to its full official name ("the Met" becomes "Metropolitan Museum of Art"); the tool looks up addresses, intersections, landmarks and businesses. If a tool returns needs_clarification, the name matches several places: ask which one in a short list (the page also shows buttons), and do not guess. When they answer, call the tool again with that option's `location` value exactly. If a tool says a name matches many places (a chain or a long street), ask for a neighborhood or cross street. Write intersections with full street names, e.g. 'Amsterdam Avenue & West 116th Street'.
- If you do not know where the user is, ask one short question. If their message contains "[my GPS location: lat,lon]", use those coordinates as the location.
- Every user message ends with a system note giving the current NYC time. Use it to turn "now", "tonight", "tomorrow morning" into an ISO 8601 `when` such as 2026-10-05T01:00. Omit `when` for "right now".
- Distances: leave `radius_m` out unless the user gave one; the tool widens by itself. If they did ("within 5 blocks"), convert (a short block is about 80 m, an avenue block about 270 m). When nothing is found, say how far you searched and offer to look further.
- The page shows a card for every result with its walking time, status, hours, features and directions, so do not repeat those details. Recommend the single best option by name in a sentence or two and say why it is the best (for example, the only one confirmed to meet their needs). Rank options the way the page does, so your pick is its first card: places that meet all the user's needs (no unconfirmed_needs) first, then places whose hours say open (status or listed_hours_status "open"), then uncertain hours, then the shortest walk.
- If no search result is confirmed open (status "open"), the page already tells the user that and suggests going home if they can; do not say either. If any search returned results, never say there are no options or that the search came up empty: name the first one as a backup.
- Be honest about uncertainty. If status is "unclear", say why in plain words and give a backup. Call such a place uncertain, never closed, and do not say "everything is closed" when any result is uncertain. For community-listed places, mention how old the listing is and that their hours come from OpenStreetMap and are not verified. Walking times are estimates.
- Only mention details that appear in tool results; do not add addresses or cross streets, and do not suggest places or kinds of places the tools did not return.
- If a tool returns an error, use its advice: fix the argument and retry once, or tell the user plainly what is unavailable. Never invent a result.
- Do not paste URLs; the page shows map buttons from tool results. Plain text, no markdown tables, no more than a few short sentences.
- Politely decline anything unrelated to finding a restroom in NYC."""

# --- The Harness ---


def _complete_with_retry(messages: list[dict]):
    """litellm.completion, retrying briefly on transient 429s (shared Vertex AI quota) before giving up."""
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        try:
            return litellm.completion(
                model="vertex_ai/gemini-3.5-flash-lite",
                vertex_location="global",
                messages=messages,
                tools=TOOLS,
            )
        except litellm.RateLimitError:
            if attempt == RATE_LIMIT_RETRIES:
                raise  # out of retries: chat() turns this into a message for the user
            time.sleep(RATE_LIMIT_BACKOFF_S * (attempt + 1))


def run_agent(messages: list[dict], state: dict) -> tuple[str, list[dict]]:
    """Complete until the model answers without asking for a tool.

    `state` is this session's meta state (remembered needs). It is passed to the tools,
    not shown to the model. Returns the final text and every tool call made along the way.
    """
    tool_calls = []

    for _ in range(MAX_TOOL_ROUNDS):
        reply = _complete_with_retry(messages).choices[0].message

        # Append assistant's reply (text, tool calls, or both) to the context.
        # model_dump() keeps it a plain dict: the raw object carries provider-specific
        # fields that trip Pydantic when LiteLLM re-serializes it next round.
        messages += [reply.model_dump()]

        # A reply with no tool calls is the final answer
        if not reply.tool_calls:
            return reply.content or "", tool_calls

        # The harness, not the model, runs each tool and appends the result
        for call in reply.tool_calls:
            # Every tool call gets a result, even unreadable arguments: the model can retry,
            # and the history never holds a tool call with no answer
            try:
                args = json.loads(call.function.arguments or "{}")
                result = run_tool(call.function.name, args, state)
            except json.JSONDecodeError:
                args = {}
                result = json.dumps({"error": (
                    "Arguments were not valid JSON. Retry with a JSON object matching the tool's parameters."
                )})
            # Recorded for the /chat response, so the page can show its work
            tool_calls += [{"name": call.function.name, "args": args, "result": result}]

            messages += [{"role": "tool", "tool_call_id": call.id, "content": result}]

    # Still asking for tools after the last round: stop here rather than loop forever
    return "Sorry, I hit my tool-call limit before finishing. Try asking in a simpler way.", tool_calls


# --- Session Store ---

# In-memory, single process. Both are keyed by session_id, which keeps chats separate.
sessions: dict[str, list] = {}  # the LLM context: every message so far
session_state: dict[str, dict] = {}  # meta state the tools read and write (saved needs)

# --- FastAPI App ---

app = FastAPI()


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


class ChatResponse(BaseModel):
    response: str
    session_id: str
    tool_calls: list[dict]


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "index.html")


@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest):
    # Get or create the session
    session_id = request.session_id or str(uuid.uuid4())
    if session_id not in sessions:
        sessions[session_id] = [{"role": "system", "content": SYSTEM_PROMPT}]
        session_state[session_id] = {}

    # The model has no clock, so stamp each user turn with the current NYC time
    now = datetime.now(NYC_TZ).strftime("%A %Y-%m-%d %H:%M")
    user_turn = f"{request.message}\n\n[system note: current NYC time is {now}]"

    # Append user's message to the context
    sessions[session_id] += [{"role": "user", "content": user_turn}]

    try:
        response, tool_calls = run_agent(sessions[session_id], session_state[session_id])
    except Exception as e:
        # Auth, billing, a model that is not running: show it in the chat, not as a 500.
        response, tool_calls = f"Model call failed: {type(e).__name__}: {str(e)[:300]}", []

    return ChatResponse(response=response, session_id=session_id, tool_calls=tool_calls)


@app.post("/clear")
def clear(session_id: str | None = None):
    # "Start over" in the page: forget the conversation and the saved needs
    sessions.pop(session_id, None)
    session_state.pop(session_id, None)
    return {"status": "ok"}


if __name__ == "__main__":
    # Local use only; Cloud Run starts the app with its own uvicorn command (see README)
    uvicorn.run(app, host="127.0.0.1", port=8000)
