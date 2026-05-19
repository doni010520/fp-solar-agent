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
