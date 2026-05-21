"""
CRUD de leads e messages. Acesso direto via SQLAlchemy async.
"""

from datetime import datetime
import uuid
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.core.db import AsyncSessionLocal
from app.models import Lead, Message


async def get_or_create_lead(phone: str, push_name: str = "") -> Lead:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Lead).where(Lead.telefone == phone))
        lead = result.scalar_one_or_none()
        if lead:
            if push_name and not lead.push_name:
                lead.push_name = push_name
                await session.commit()
                await session.refresh(lead)
            return lead

        lead = Lead(telefone=phone, push_name=push_name or None)
        session.add(lead)
        await session.commit()
        await session.refresh(lead)
        logger.info(f"[lead] criado phone={phone}")
        return lead


async def update_lead(phone: str, **fields) -> Lead | None:
    if not fields:
        return None
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Lead).where(Lead.telefone == phone))
        lead = result.scalar_one_or_none()
        if not lead:
            return None
        for k, v in fields.items():
            if hasattr(lead, k) and v is not None:
                setattr(lead, k, v)
        await session.commit()
        await session.refresh(lead)
        return lead


async def disable_ia(phone: str, reason: str) -> None:
    await update_lead(
        phone,
        ia_on_off="OFF",
        status_funil_vendas=reason,  # "transferido_para_time" ou "atendimento_humano"
        etapa_follow_up=reason,
    )
    logger.info(f"[lead] IA OFF phone={phone} reason={reason}")


async def touch_last_contact(phone: str) -> None:
    await update_lead(phone, ultimo_contato=datetime.utcnow())


# ── Messages ───────────────────────────────────────────────

async def save_message(
    lead_id: uuid.UUID,
    role: str,
    content: str,
    *,
    tool_name: str | None = None,
    tool_args: dict | None = None,
    message_id_wpp: str | None = None,
    message_type: str = "text",
) -> Message:
    async with AsyncSessionLocal() as session:
        msg = Message(
            lead_id=lead_id,
            role=role,
            content=content,
            tool_name=tool_name,
            tool_args=tool_args,
            message_id_wpp=message_id_wpp,
            message_type=message_type,
        )
        session.add(msg)
        await session.commit()
        await session.refresh(msg)
        return msg


async def set_message_wpp_id(message_pk: uuid.UUID, wpp_id: str) -> None:
    """Grava o messageid retornado pela uazapi após enviar a mensagem.
    Usado pra diferenciar echo da própria IA vs humano assumindo a conversa."""
    if not wpp_id:
        return
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Message).where(Message.id == message_pk))
        msg = result.scalar_one_or_none()
        if msg:
            msg.message_id_wpp = wpp_id
            await session.commit()


async def is_our_outbound_message(wpp_id: str, phone: str | None = None, window_seconds: int = 60) -> bool:
    """True se esse fromMe=true é provavelmente eco da própria Lara.

    Estratégia dupla pra evitar race condition na escrita do message_id_wpp:
    1. Match exato no message_id_wpp (caminho ideal, quando set_message_wpp_id
       já gravou)
    2. Fallback temporal: se o lead recebeu QUALQUER resposta assistant nos
       últimos `window_seconds`, presumimos que esse fromMe é eco.
    """
    if wpp_id:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Message).where(
                    Message.message_id_wpp == wpp_id,
                    Message.role == "assistant",
                )
            )
            if result.scalar_one_or_none():
                return True

    if not phone:
        return False

    from datetime import datetime, timedelta, timezone
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=window_seconds)
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Message)
            .join(Lead, Message.lead_id == Lead.id)
            .where(
                Lead.telefone == phone,
                Message.role == "assistant",
                Message.created_at >= cutoff,
            )
            .limit(1)
        )
        return result.scalar_one_or_none() is not None


async def load_history(lead_id: uuid.UUID, limit: int = 30) -> list[Message]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Message)
            .where(Message.lead_id == lead_id)
            .order_by(Message.created_at.desc())
            .limit(limit)
        )
        msgs = list(result.scalars().all())
        msgs.reverse()
        return msgs
