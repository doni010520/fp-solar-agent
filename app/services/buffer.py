"""
Buffer in-memory de mensagens com debounce assíncrono.

Junta mensagens enviadas em rajada por um mesmo telefone numa única chamada ao LLM.
Se o cliente envia 3 msgs em 5s, esperamos `debounce_seconds` de silêncio antes de
processar tudo de uma vez.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable
from loguru import logger
from app.core.config import get_settings

settings = get_settings()


@dataclass
class PendingMessage:
    body: str
    message_id: str
    message_type: str  # text | audio | image | document
    media_url: str | None = None  # preenchido pelo media_processor antes do flush
    push_name: str = ""


@dataclass
class _BufferState:
    messages: list[PendingMessage] = field(default_factory=list)
    task: asyncio.Task | None = None


class MessageBuffer:
    """Debouncer por telefone. Single-process, single-worker."""

    def __init__(self, debounce_seconds: int | None = None):
        self._states: dict[str, _BufferState] = {}
        self._lock = asyncio.Lock()
        self.debounce = debounce_seconds or settings.buffer_debounce_seconds

    async def add(
        self,
        phone: str,
        msg: PendingMessage,
        on_flush: Callable[[str, list[PendingMessage]], Awaitable[None]],
    ) -> None:
        async with self._lock:
            state = self._states.setdefault(phone, _BufferState())
            state.messages.append(msg)
            if state.task and not state.task.done():
                state.task.cancel()
            state.task = asyncio.create_task(self._flush_after(phone, on_flush))

    async def _flush_after(
        self,
        phone: str,
        on_flush: Callable[[str, list[PendingMessage]], Awaitable[None]],
    ) -> None:
        try:
            await asyncio.sleep(self.debounce)
        except asyncio.CancelledError:
            return

        async with self._lock:
            state = self._states.pop(phone, None)

        if not state or not state.messages:
            return

        logger.info(f"[buffer] flush phone={phone} count={len(state.messages)}")
        try:
            await on_flush(phone, state.messages)
        except Exception as e:
            logger.exception(f"[buffer] on_flush error for {phone}: {e}")


buffer = MessageBuffer()
