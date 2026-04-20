"""
Chat assistant API — natural-language → dashboard API calls via local Ollama.

v1 scope: read-only GET endpoints only. The tool registry is curated in
services/chat_tools.py. Writes/deletes will need a confirmation UX before
being added.
"""
import asyncio
import json
import logging
import time
from typing import Any, Literal, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..config import settings
from ..database import User
from ..services import ollama_service
from ..services.chat_tools import (
    TOOLS_BY_NAME,
    all_tool_specs,
    describe,
    resolve_request,
)
from ..services.chat_pending import pending_actions
from .auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat", tags=["chat"])


# ── Request / response models ────────────────────────────────────────────────

class ChatMessage(BaseModel):
    role: Literal["user", "assistant", "system", "tool"]
    content: str = ""
    # Echoed back when role == "tool"; identifies which tool call this result is for.
    tool_call_id: Optional[str] = None
    name: Optional[str] = None
    # Set on assistant turns the server emitted last round so the model can see
    # which tools it called. Round-tripped opaquely from the previous /message
    # response — the frontend should not synthesise these.
    tool_calls: Optional[list[dict]] = None


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    history: list[ChatMessage] = Field(default_factory=list)


# ── System prompt ────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are the assistant embedded inside the Infrastructure Management Dashboard.

You help the logged-in user answer questions about their virtual machines, cloud instances, \
background jobs, and container workloads by calling the provided tools.

# How to think about a request

1. Identify what the user wants: a count, a list, details about a specific item, or a status check.
2. Pick the single tool whose description best matches — read the "Examples:" line in each \
tool description; those are paraphrases of real user phrasings.
3. If the request is ambiguous between two tools, prefer the more specific one \
(e.g. get_running_vms over list_vms when the user only cares about running VMs; \
get_dashboard_stats over list_vms when the user only wants a count).
4. Map informal terms to the right surface:
   - "machines", "VMs", "boxes" → VM tools (list_vms / get_running_vms / get_dashboard_stats)
   - "EC2", "AWS instances", "cloud VMs in AWS" → list_aws_instances
   - "Azure VMs", "cloud VMs in Azure" → list_azure_vms
   - "deploys", "runs", "tasks", "background work" → list_jobs
   - "containers", "docker", "Portainer" → list_containers
   - "start/power on/boot" a VM → start_vm; "stop/shut down/power off" a VM → stop_vm
   - "cancel/kill/stop" a job → cancel_job
5. After a tool returns, summarise in plain English. Be concrete with numbers and names. \
Do NOT dump raw JSON unless asked.

# Rules

- Always call a tool when the user asks for live state, counts, lists, OR actions \
(start/stop/cancel). Do not guess or recall — your training data is stale.
- Only call tools that are provided. Never invent tool names.
- If no tool fits, say so plainly. Do not fabricate data.
- All tool calls run with the user's own permissions. If a tool returns an error about \
access or permissions, tell the user they don't have access — do NOT retry with \
different arguments hoping it'll work.
- Tool arguments must match the schema exactly. If a parameter has an enum, use one of \
the listed values verbatim. Do not invent parameters that aren't in the schema.

# Mutating actions (start_vm / stop_vm / cancel_job)

- Mutating tools do NOT execute when you call them. The dashboard UI intercepts the \
call and shows a Confirm/Cancel card to the user. Your job is just to emit the right \
tool call with the right arguments.
- Do NOT ask the user "are you sure?" before calling a mutating tool — the UI handles \
confirmation. Just call the tool.
- For start_vm / stop_vm you MUST pass the exact vmx_path. If the user refers to a VM \
by name (e.g. "web-01"), first call list_vms to find the matching vmx_path, then call \
start_vm/stop_vm with that path. Never invent a path.
- If the user's reference is ambiguous (multiple VMs match the name), list the matches \
and ask which one instead of calling the mutating tool.

# Worked examples

User: How many VMs do we have running?
Assistant calls: get_dashboard_stats()
Tool returns: {"total": 42, "running": 17, "stopped": 23, "offline": 2}
Assistant: 17 of your 42 VMs are currently running. 23 are stopped and 2 are offline.

User: Show me the failed jobs.
Assistant calls: list_jobs(status="failed", page_size=20)
Tool returns: {"jobs": [{"id":"a1b2","job_type":"ec2_deploy","created_by":"alice",...}, ...], "total": 3}
Assistant: There are 3 failed jobs. The most recent is an EC2 deploy by alice (a1b2). Want details on any of them?

User: What's deployed in AWS?
Assistant calls: list_aws_instances()
Tool returns: [{"instance_id":"i-abc","state":"running","instance_type":"t3.medium",...}, ...]
Assistant: You have 4 EC2 instances deployed via the dashboard: 3 running, 1 stopped. The running ones are all t3.medium.

User: Tell me about job a1b2c3d4-...
Assistant calls: get_job(job_id="a1b2c3d4-...")
Tool returns: {"id":"a1b2c3d4-...","status":"failed","error_message":"AMI not found",...}
Assistant: Job a1b2c3d4 (an EC2 deploy) failed because the AMI was not found. It ran for 12 seconds before failing.

User: What's the weather like?
Assistant: I can only help with infrastructure questions — VMs, jobs, AWS, Azure, and containers. Is there something I can check on those?

User: Stop web-01.
Assistant calls: list_vms()
Tool returns: [{"name":"web-01","vmx_path":"C:/VMs/Hydra/web-01/web-01.vmx","workgroup":"Hydra",...}, ...]
Assistant calls: stop_vm(vmx_path="C:/VMs/Hydra/web-01/web-01.vmx")
(The UI now shows a Confirm card. Do not narrate — your work for this turn is done.)

User: Kill job a1b2c3d4.
Assistant calls: cancel_job(job_id="a1b2c3d4")
(The UI now shows a Confirm card.)
"""


# ── Tool invocation ──────────────────────────────────────────────────────────

def _internal_base_url() -> str:
    # The chat router calls back into the same app over HTTP so that the
    # target endpoints re-apply their own auth/permission/workgroup deps
    # under the caller's token, identical to a browser request.
    return f"http://localhost:{settings.api_port}"


async def _invoke_tool(
    client: httpx.AsyncClient,
    tool_name: str,
    arguments: dict,
    bearer_token: str,
) -> tuple[int, Any]:
    """Call the dashboard endpoint backing this tool; return (status, body)."""
    tool = TOOLS_BY_NAME.get(tool_name)
    if tool is None:
        return 400, {"error": f"unknown tool {tool_name!r}"}

    try:
        path, query, body_json = resolve_request(tool, arguments)
    except ValueError as exc:
        return 400, {"error": str(exc)}

    url = f"{_internal_base_url()}{path}"
    headers = {"Authorization": f"Bearer {bearer_token}"}
    method = tool.method.upper()

    try:
        if method == "GET":
            resp = await client.get(url, params=query, headers=headers, timeout=60.0)
        elif method == "POST":
            resp = await client.post(
                url, params=query, json=body_json or {}, headers=headers, timeout=60.0,
            )
        elif method == "DELETE":
            resp = await client.delete(url, params=query, headers=headers, timeout=60.0)
        else:
            return 400, {"error": f"unsupported tool method {method!r}"}
    except httpx.HTTPError as exc:
        logger.warning("chat tool %s HTTP error: %s", tool_name, exc)
        return 502, {"error": f"internal call failed: {exc}"}

    try:
        body = resp.json()
    except ValueError:
        body = {"raw": resp.text[:2000]}
    return resp.status_code, body


def _truncate_tool_result(body: Any, max_chars: int = 6000) -> str:
    """Serialise a tool result for the model, with a hard size cap."""
    s = json.dumps(body, default=str)
    if len(s) <= max_chars:
        return s
    return s[:max_chars] + f"\n…(truncated, original was {len(s)} chars)"


# ── SSE helpers ──────────────────────────────────────────────────────────────

def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n".encode("utf-8")


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/tools")
async def list_chat_tools(_user: User = Depends(get_current_user)):
    """Return the active tool registry (for admin/debug views)."""
    return {
        "enabled": settings.chat_enabled,
        "model": settings.chat_model,
        "tools": all_tool_specs(),
    }


@router.post("/message")
async def chat_message(
    req: ChatRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """
    Stream the assistant's response to a single user message via SSE.

    Event stream:
      - tool_call    — the model requested a tool (emitted before the call runs)
      - tool_result  — the tool call returned (status + short summary)
      - message      — final assistant text
      - done         — terminator with aggregated timing/token counts
      - error        — fatal error; stream ends after
    """
    if not settings.chat_enabled:
        raise HTTPException(status_code=503, detail="Chat assistant is disabled.")

    # We need the caller's raw bearer token to forward to internal endpoints.
    # OAuth2PasswordBearer inside get_current_user already validated it; we
    # just re-read from the header here instead of threading it through.
    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    bearer_token = auth_header.split(" ", 1)[1].strip()

    tool_specs = all_tool_specs()

    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    for m in req.history:
        entry: dict[str, Any] = {"role": m.role, "content": m.content}
        if m.name:
            entry["name"] = m.name
        if m.tool_calls:
            entry["tool_calls"] = m.tool_calls
        messages.append(entry)
    messages.append({"role": "user", "content": req.message})

    async def _stream():
        t_started = time.monotonic()
        total_prompt_tokens = 0
        total_completion_tokens = 0

        try:
            async with httpx.AsyncClient() as internal_client:
                for iteration in range(settings.chat_max_tool_calls + 1):
                    # Call the model. Offer tools on every turn; cheap and lets the
                    # model chain a follow-up call if needed (capped by the loop).
                    result = await ollama_service.chat(
                        messages=messages,
                        tools=tool_specs,
                    )
                    total_prompt_tokens += int(result.get("prompt_eval_count") or 0)
                    total_completion_tokens += int(result.get("eval_count") or 0)

                    msg = result.get("message") or {}
                    tool_calls = msg.get("tool_calls") or []
                    content = msg.get("content") or ""

                    if not tool_calls:
                        # Model has produced its final natural-language answer.
                        yield _sse("message", {"content": content})
                        break

                    if iteration == settings.chat_max_tool_calls:
                        # We hit the cap with another tool call pending — stop and
                        # return whatever content we have (usually empty) + a note.
                        yield _sse("message", {
                            "content": content or (
                                "I reached the tool-call limit for this message. "
                                "Try asking a narrower question."
                            ),
                        })
                        break

                    # Record the assistant turn (with tool calls) in history before
                    # appending tool results so the next model turn has full context.
                    messages.append({
                        "role": "assistant",
                        "content": content,
                        "tool_calls": tool_calls,
                    })

                    mutation_pending = False
                    for call in tool_calls:
                        fn = call.get("function") or {}
                        name = fn.get("name", "")
                        args = fn.get("arguments") or {}
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except json.JSONDecodeError:
                                args = {}

                        tool = TOOLS_BY_NAME.get(name)
                        if tool is not None and tool.requires_confirmation:
                            # Pause the loop: stash the call and let the UI render a
                            # Confirm/Cancel card. The suspended conversation is not
                            # resumed — after confirmation the frontend shows the
                            # raw result and the next user message starts fresh.
                            pending = await pending_actions.put(
                                username=current_user.username,
                                tool_name=name,
                                arguments=args,
                                description=describe(tool, args),
                            )
                            logger.info(
                                "chat pending_action user=%s tool=%s action_id=%s",
                                current_user.username, name, pending.action_id,
                            )
                            yield _sse("pending_action", {
                                "action_id": pending.action_id,
                                "tool_name": name,
                                "arguments": args,
                                "description": pending.description,
                                "preamble": content,
                            })
                            mutation_pending = True
                            break

                        yield _sse("tool_call", {"name": name, "arguments": args})

                        status, body = await _invoke_tool(
                            internal_client, name, args, bearer_token
                        )
                        logger.info(
                            "chat tool name=%s status=%d user=%s",
                            name, status, current_user.username,
                        )
                        yield _sse("tool_result", {
                            "name": name,
                            "status": status,
                            "ok": 200 <= status < 300,
                        })

                        messages.append({
                            "role": "tool",
                            "name": name,
                            "content": _truncate_tool_result(
                                body if 200 <= status < 300
                                else {"error": body, "http_status": status}
                            ),
                        })

                    if mutation_pending:
                        # Drop the orphan assistant-with-tool_calls turn from
                        # history — there is no matching tool result, and re-
                        # feeding it next round would confuse the model.
                        if messages and messages[-1].get("role") == "assistant":
                            messages.pop()
                        break

            # Strip the system prompt before handing history back to the client;
            # the next request will re-prepend it server-side.
            history_out = [m for m in messages if m.get("role") != "system"]
            yield _sse("history", {"messages": history_out})

            elapsed_ms = int((time.monotonic() - t_started) * 1000)
            logger.info(
                "chat done user=%s prompt_tokens=%d completion_tokens=%d wall_ms=%d",
                current_user.username, total_prompt_tokens,
                total_completion_tokens, elapsed_ms,
            )
            yield _sse("done", {
                "prompt_tokens": total_prompt_tokens,
                "completion_tokens": total_completion_tokens,
                "wall_ms": elapsed_ms,
            })

        except ollama_service.OllamaError as exc:
            logger.error("chat ollama error user=%s: %s", current_user.username, exc)
            yield _sse("error", {"detail": f"Model backend unavailable: {exc}"})
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover — defensive
            logger.exception("chat unexpected error user=%s", current_user.username)
            yield _sse("error", {"detail": f"Unexpected error: {exc}"})

    return StreamingResponse(_stream(), media_type="text/event-stream")


# ── Confirmation endpoint (phase 2 of the two-phase mutation flow) ───────────

class ConfirmRequest(BaseModel):
    action_id: str = Field(..., min_length=1, max_length=64)


@router.post("/confirm")
async def chat_confirm(
    req: ConfirmRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """
    Execute a previously-proposed mutating tool call after the user clicked
    Confirm in the UI. Returns the raw tool result; the UI renders it without
    feeding it back to the model (keeps latency low + avoids hallucinated
    post-hoc narration of actions that did not actually succeed).
    """
    if not settings.chat_enabled:
        raise HTTPException(status_code=503, detail="Chat assistant is disabled.")

    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    bearer_token = auth_header.split(" ", 1)[1].strip()

    pending = await pending_actions.claim(req.action_id, current_user.username)
    if pending is None:
        # Deliberately opaque: expired, unknown, or owned by someone else all
        # collapse to the same response so we don't leak whether an ID existed.
        raise HTTPException(
            status_code=410,
            detail="This action is no longer available. It may have expired — ask again.",
        )

    async with httpx.AsyncClient() as client:
        status, body = await _invoke_tool(
            client, pending.tool_name, pending.arguments, bearer_token
        )

    ok = 200 <= status < 300
    logger.info(
        "chat confirm user=%s tool=%s status=%d ok=%s",
        current_user.username, pending.tool_name, status, ok,
    )
    return {
        "ok": ok,
        "status": status,
        "tool_name": pending.tool_name,
        "description": pending.description,
        "result": body,
    }


@router.post("/cancel")
async def chat_cancel(
    req: ConfirmRequest,
    current_user: User = Depends(get_current_user),
):
    """User declined a pending mutation — drop it from the store."""
    pending = await pending_actions.claim(req.action_id, current_user.username)
    # Whether or not it existed, the outcome for the user is the same: gone.
    return {"cancelled": pending is not None}
