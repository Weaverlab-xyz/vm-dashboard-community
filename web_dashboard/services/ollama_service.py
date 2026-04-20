"""
Async httpx client for a local Ollama server.

Used by api/chat.py to drive the natural-language assistant. Only the tool-
calling chat endpoint is wrapped here — Ollama's `/api/chat` is OpenAI-shaped
for llama3.1 and friends, so the rest of the code can speak that dialect.
"""
import logging
from typing import Any, Optional

import httpx

from ..config import settings

logger = logging.getLogger(__name__)


class OllamaError(Exception):
    """Raised when Ollama returns an error or is unreachable."""


async def chat(
    messages: list[dict],
    tools: Optional[list[dict]] = None,
    model: Optional[str] = None,
    timeout: Optional[float] = None,
) -> dict:
    """
    Call Ollama's /api/chat and return the single (non-streamed) response.

    Returns the parsed JSON body. The caller cares about:
      - result["message"]["content"]      — assistant text (may be empty during tool use)
      - result["message"]["tool_calls"]   — list of {function: {name, arguments}}
      - result["prompt_eval_count"]       — input tokens
      - result["eval_count"]              — output tokens
      - result["total_duration"]          — nanoseconds
    """
    url = f"{settings.ollama_base_url.rstrip('/')}/api/chat"
    payload: dict[str, Any] = {
        "model": model or settings.chat_model,
        "messages": messages,
        "stream": False,
        "options": {
            # Low temperature for consistent tool-call JSON
            "temperature": 0.2,
        },
    }
    if tools:
        payload["tools"] = tools

    t = timeout if timeout is not None else settings.chat_request_timeout
    try:
        async with httpx.AsyncClient(timeout=t) as client:
            resp = await client.post(url, json=payload)
    except httpx.HTTPError as exc:
        # str(TimeoutException) is empty — include the class name so the
        # surfaced error tells the user *what* went wrong.
        detail = str(exc) or type(exc).__name__
        if isinstance(exc, httpx.TimeoutException):
            detail = f"{detail} after {t:.0f}s (model may still be loading or prompt is too large for CPU inference)"
        raise OllamaError(f"Ollama request failed: {detail}") from exc

    if resp.status_code != 200:
        raise OllamaError(f"Ollama returned HTTP {resp.status_code}: {resp.text[:500]}")

    try:
        return resp.json()
    except ValueError as exc:
        raise OllamaError(f"Ollama returned non-JSON body: {resp.text[:500]}") from exc


async def healthcheck() -> bool:
    """Return True if Ollama is reachable and the configured model is present."""
    url = f"{settings.ollama_base_url.rstrip('/')}/api/tags"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url)
        if resp.status_code != 200:
            return False
        data = resp.json()
        names = {m.get("name", "") for m in data.get("models", [])}
        return any(settings.chat_model in n or n.startswith(settings.chat_model) for n in names)
    except Exception as exc:
        logger.debug("ollama healthcheck failed: %s", exc)
        return False
