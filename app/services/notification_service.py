"""
Formata e envia notificações ao grupo interno da FP Solar (uazapi).

Espelha o `notificar_time_fpsolar` do n8n, mas substitui o LLM-formatador
por templates Python (zero custo, mesmo visual).

Eventos disparados pelas tools da Lara:
- notify_qualified_lead → ⭐ Novo Lead Qualificado
- request_human        → 🚨 Atendimento Humano Urgente
"""

from datetime import datetime
import uuid
from sqlalchemy import select
from loguru import logger

from app.core.config import get_settings
from app.core.db import AsyncSessionLocal
from app.models import Lead, Notification
from app.services.uazapi import uazapi
from app.services.contact_updater import disable_ia

settings = get_settings()


def _fmt_field(value: str | None) -> str:
    return value if value else "Não informado"


def _format_qualified_lead(lead: Lead, resumo: str) -> str:
    return (
        f"⭐ *Novo Lead Qualificado para a FP Solar!* ⭐\n\n"
        f"Um novo cliente completou a qualificação e está pronto para receber a proposta.\n\n"
        f"*Dados de Contato:*\n"
        f"👤 *Nome:* {_fmt_field(lead.full_name)}\n"
        f"🪪 *CPF:* {_fmt_field(lead.cpf)}\n"
        f"🗓️ *Data de nascimento:* {_fmt_field(lead.data_nascimento.isoformat() if lead.data_nascimento else None)}\n"
        f"📞 *Telefone:* {lead.telefone}\n"
        f"📧 *E-mail:* {_fmt_field(lead.email)}\n\n"
        f"📝 *Resumo da Qualificação:*\n{resumo}\n\n"
        f"➡️ *Ação Necessária:* Preparar a proposta e entrar em contato com o cliente."
    )


def _format_human_request(lead: Lead, resumo: str) -> str:
    return (
        f"🚨 *Atendimento Humano Urgente!* 🚨\n\n"
        f"Um cliente solicitou falar com um atendente da FP Solar.\n\n"
        f"*Dados de Contato:*\n"
        f"👤 *Nome:* {_fmt_field(lead.full_name or lead.push_name)}\n"
        f"📞 *Telefone:* {lead.telefone}\n\n"
        f"📝 *Resumo do pedido:*\n{resumo}\n\n"
        f"➡️ *Ação Necessária:* Assumir o atendimento imediatamente."
    )


async def _send_and_log(
    lead_id: uuid.UUID,
    tipo: str,
    mensagem: str,
    payload: dict,
) -> bool:
    result = await uazapi.send_text(settings.internal_group_id, mensagem, delay=3000)
    sucesso = result is not None
    erro = None if sucesso else "uazapi retornou None"

    async with AsyncSessionLocal() as session:
        notif = Notification(
            lead_id=lead_id,
            tipo=tipo,
            payload=payload,
            mensagem=mensagem,
            sucesso=sucesso,
            erro=erro,
        )
        session.add(notif)
        await session.commit()

    if sucesso:
        logger.info(f"[notif] enviado tipo={tipo} lead_id={lead_id}")
    else:
        logger.error(f"[notif] FALHA tipo={tipo} lead_id={lead_id}")
    return sucesso


async def notify_qualified_lead(
    phone: str,
    *,
    nome: str | None = None,
    email: str | None = None,
    cpf: str | None = None,
    data_de_nascimento: str | None = None,
    resumo_da_solicitacao: str,
) -> dict:
    """Tool executada pela Lara quando qualificação está completa."""
    from app.services.contact_updater import update_lead

    updates: dict = {}
    if nome:
        updates["full_name"] = nome
    if email:
        updates["email"] = email
    if cpf:
        updates["cpf"] = cpf
    if data_de_nascimento:
        try:
            updates["data_nascimento"] = datetime.strptime(data_de_nascimento, "%Y-%m-%d").date()
        except ValueError:
            try:
                updates["data_nascimento"] = datetime.strptime(data_de_nascimento, "%d/%m/%Y").date()
            except ValueError:
                logger.warning(f"data_de_nascimento inválida: {data_de_nascimento}")
    if updates:
        await update_lead(phone, **updates)

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Lead).where(Lead.telefone == phone))
        lead = result.scalar_one_or_none()
    if not lead:
        return {"ok": False, "error": "lead_not_found"}

    mensagem = _format_qualified_lead(lead, resumo_da_solicitacao)
    payload = {
        "nome": nome,
        "email": email,
        "cpf": cpf,
        "data_de_nascimento": data_de_nascimento,
        "telefone": phone,
        "resumo_da_solicitacao": resumo_da_solicitacao,
    }
    ok = await _send_and_log(lead.id, "qualified_lead", mensagem, payload)
    await disable_ia(phone, "transferido_para_time")
    return {"ok": ok}


async def request_human(phone: str, *, resumo_do_pedido: str) -> dict:
    """Tool executada pela Lara quando cliente pede atendimento humano."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Lead).where(Lead.telefone == phone))
        lead = result.scalar_one_or_none()
    if not lead:
        return {"ok": False, "error": "lead_not_found"}

    mensagem = _format_human_request(lead, resumo_do_pedido)
    payload = {"telefone": phone, "resumo_do_pedido": resumo_do_pedido}
    ok = await _send_and_log(lead.id, "human_request", mensagem, payload)
    await disable_ia(phone, "atendimento_humano")
    return {"ok": ok}
