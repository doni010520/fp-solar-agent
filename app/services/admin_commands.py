"""
Comandos administrativos enviados pela própria conversa do WhatsApp.

Interceptados ANTES do LLM. Casos típicos:
- /limpar          → apaga histórico do lead (Lara começa do zero)
- /reset           → apaga histórico + reseta dados do lead (novo, IA on)
- /ia on | off     → liga/desliga a IA pra esse telefone

Retorna a resposta a enviar OU None se não for comando.
"""

from sqlalchemy import delete, select
from loguru import logger

from app.core.db import AsyncSessionLocal
from app.models import Lead, Message


COMMAND_PREFIXES = ("/", "!")


def is_command(text: str) -> bool:
    t = text.strip().lower()
    return any(t.startswith(p) for p in COMMAND_PREFIXES)


async def handle(phone: str, text: str) -> str | None:
    """Retorna a resposta de texto (str) ou None se não for comando reconhecido."""
    cmd = text.strip().lower()

    if cmd in ("/limpar", "!limpar", "/clear"):
        return await _clear_history(phone)

    if cmd in ("/reset", "!reset", "/reiniciar", "!reiniciar"):
        return await _full_reset(phone)

    if cmd in ("/ia on", "!ia on", "/ia_on"):
        return await _toggle_ia(phone, "ON")

    if cmd in ("/ia off", "!ia off", "/ia_off"):
        return await _toggle_ia(phone, "OFF")

    if cmd in ("/help", "!help", "/ajuda", "!ajuda"):
        return (
            "🛠️ *Comandos de admin disponíveis:*\n\n"
            "• `/limpar` — apaga histórico da conversa\n"
            "• `/reset` — apaga histórico + reseta seu cadastro (começa do zero)\n"
            "• `/ia off` — pausa a Lara pra esse número\n"
            "• `/ia on` — religa a Lara\n"
            "• `/ajuda` — mostra esta mensagem"
        )

    return None  # não é um comando que reconhecemos


# ── Implementações ────────────────────────────────────────────

async def _clear_history(phone: str) -> str:
    async with AsyncSessionLocal() as s:
        lead = (await s.execute(select(Lead).where(Lead.telefone == phone))).scalar_one_or_none()
        if not lead:
            return "Nenhum histórico encontrado pra esse número."
        result = await s.execute(delete(Message).where(Message.lead_id == lead.id))
        await s.commit()
        deleted = result.rowcount or 0
    logger.info(f"[admin] /limpar phone={phone} deleted={deleted}")
    return f"🧹 Histórico apagado ({deleted} mensagens). Pode mandar a próxima mensagem do zero."


async def _full_reset(phone: str) -> str:
    async with AsyncSessionLocal() as s:
        lead = (await s.execute(select(Lead).where(Lead.telefone == phone))).scalar_one_or_none()
        if not lead:
            return "Nenhum cadastro encontrado pra esse número."
        result = await s.execute(delete(Message).where(Message.lead_id == lead.id))
        deleted = result.rowcount or 0
        # Reset dos campos coletados (mantém telefone, push_name, id)
        lead.full_name = None
        lead.email = None
        lead.cpf = None
        lead.data_nascimento = None
        lead.tipo_projeto = None
        lead.tipo_telhado = None
        lead.padrao_energia = None
        lead.cidade = None
        lead.valor_conta_luz = None
        lead.observacoes = None
        lead.status_funil_vendas = "novo"
        lead.etapa_follow_up = "aguardando_primeira_mensagem"
        lead.ia_on_off = "ON"
        await s.commit()
    logger.info(f"[admin] /reset phone={phone} deleted={deleted}")
    return f"🔄 Reset completo feito ({deleted} mensagens apagadas, cadastro zerado, IA ligada). Pode começar do zero."


async def _toggle_ia(phone: str, status: str) -> str:
    async with AsyncSessionLocal() as s:
        lead = (await s.execute(select(Lead).where(Lead.telefone == phone))).scalar_one_or_none()
        if not lead:
            return "Nenhum cadastro encontrado pra esse número."
        lead.ia_on_off = status
        await s.commit()
    logger.info(f"[admin] /ia {status} phone={phone}")
    if status == "OFF":
        return "🔕 IA pausada. Eu não vou mais responder até você mandar `/ia on`."
    return "🔔 IA religada. Pode mandar sua próxima mensagem normalmente."
