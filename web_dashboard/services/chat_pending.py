"""
In-memory pending-action store for the chat assistant.

When the model requests a mutating tool, the chat router stashes the call here
and waits for the user to confirm via POST /api/chat/confirm. Entries are:

  - one-shot (consumed when claimed),
  - owned (only the originating username can claim),
  - short-lived (60s TTL — this is a UI-interaction window, not a job).

No Redis: short-lived confirmations don't warrant a round-trip or a new
dependency. Worst case on process restart is the user clicks Confirm and gets
"action expired" — they re-ask and move on.
"""
from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass
from typing import Any

_DEFAULT_TTL_SECONDS = 60.0


@dataclass
class PendingAction:
    action_id: str
    username: str
    tool_name: str
    arguments: dict[str, Any]
    description: str
    expires_at: float


class PendingActionStore:
    def __init__(self, ttl_seconds: float = _DEFAULT_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._items: dict[str, PendingAction] = {}
        self._lock = asyncio.Lock()

    async def put(
        self,
        *,
        username: str,
        tool_name: str,
        arguments: dict[str, Any],
        description: str,
    ) -> PendingAction:
        now = time.monotonic()
        action_id = secrets.token_urlsafe(16)
        item = PendingAction(
            action_id=action_id,
            username=username,
            tool_name=tool_name,
            arguments=dict(arguments),
            description=description,
            expires_at=now + self._ttl,
        )
        async with self._lock:
            self._prune_locked(now)
            self._items[action_id] = item
        return item

    async def claim(self, action_id: str, username: str) -> PendingAction | None:
        """Fetch and remove the pending action if it exists, is unexpired, and
        belongs to `username`. Returns None otherwise — callers should treat
        None as 'expired/unknown/unauthorized' without distinguishing."""
        now = time.monotonic()
        async with self._lock:
            self._prune_locked(now)
            item = self._items.get(action_id)
            if item is None:
                return None
            if item.username != username:
                return None
            if item.expires_at < now:
                self._items.pop(action_id, None)
                return None
            # One-shot: remove on successful claim.
            self._items.pop(action_id, None)
            return item

    def _prune_locked(self, now: float) -> None:
        expired = [k for k, v in self._items.items() if v.expires_at < now]
        for k in expired:
            self._items.pop(k, None)


# Module-level singleton — one store per app process is correct; all chat
# sessions in this worker share it and pending actions do not outlive the
# process by design.
pending_actions = PendingActionStore()
