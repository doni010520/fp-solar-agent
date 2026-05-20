"""
Orquestrador da conversa. Cola buffer → mídia → openai → uazapi → persistência.

Fluxo (cada mensagem do cliente):
1. Webhook entrega evento ao FastAPI → handle_incoming()
2. handle_incoming() resolve mídia (transcrição/descrição/PDF) e adiciona ao buffer
3. Após N segundos de silêncio, buffer chama _flush()
4. _flush() carrega histórico, monta texto unificado, chama Lara (openai_service)
5. Persiste mensagens e tools executadas, envia resposta via uazapi
"""

from loguru import logger

from app.services import contact_updater, openai_service, admin_commands
from app.services.buffer import PendingMessage, buffer
from app.services.media_processor import process_media
from app.core.config import get_settings
from app.services.uazapi import uazapi

settings = get_settings()


async def handle_incoming(parsed: dict) -> None:
    """Recebe webhook já normalizado pelo UazapiClient.parse_webhook."""
    phone = parsed["phone"]
    body = parsed.get("body", "")
    msg_type = parsed.get("type", "text")
    message_id = parsed.get("message_id", "")
    push_name = parsed.get("push_name", "")

    # Allowlist (modo teste). Se ALLOWED_PHONES estiver vazio, deixa passar tudo.
    allowed = settings.allowed_phones_set
    if allowed and phone not in allowed:
        logger.info(f"[conv] phone={phone} não está na allowlist, ignorando")
        return

    lead = await contact_updater.get_or_create_lead(phone, push_name)

    # ── Admin commands (interceptam antes do LLM) ────────────
    # Funcionam mesmo com IA desligada (pra permitir /ia on, /reset, etc.)
    if msg_type == "text" and body and admin_commands.is_command(body):
        reply = await admin_commands.handle(phone, body)
        if reply is not None:
            try:
                if message_id:
                    await uazapi.mark_read(message_id)
            except Exception:
                pass
            await uazapi.send_text(phone, reply, delay=500)
            return

    if lead.ia_on_off == "OFF":
        logger.info(f"[conv] IA OFF para {phone}, ignorando mensagem")
        return

    await contact_updater.touch_last_contact(phone)

    if msg_type in ("audio", "image", "document"):
        try:
            body = await process_media(msg_type, message_id, caption=body)
        except Exception as e:
            logger.exception(f"media_processor falhou: {e}")
            body = f"[{msg_type} recebido]"
    elif msg_type not in ("text",):
        # tipos não suportados (sticker, location, etc.) — registra mas não processa
        logger.info(f"[conv] tipo não suportado ignorado: {msg_type}")
        return

    if not body:
        logger.info(f"[conv] body vazio para {phone}, ignorando")
        return

    # marca como lida (UX)
    try:
        if message_id:
            await uazapi.mark_read(message_id)
    except Exception:
        pass

    pending = PendingMessage(
        body=body,
        message_id=message_id,
        message_type=msg_type,
        push_name=push_name,
    )
    await buffer.add(phone, pending, on_flush=_flush)


async def _flush(phone: str, messages: list[PendingMessage]) -> None:
    """Chamado pelo buffer após silêncio."""
    lead = await contact_updater.get_or_create_lead(phone)

    if lead.ia_on_off == "OFF":
        logger.info(f"[flush] IA OFF para {phone}; descartando flush")
        return

    # Persiste cada mensagem do usuário
    for m in messages:
        await contact_updater.save_message(
            lead.id, "user", m.body, message_id_wpp=m.message_id, message_type=m.message_type
        )

    user_text = "\n".join(m.body for m in messages).strip()
    history = await contact_updater.load_history(lead.id, limit=30)
    # Remove a última (mensagem que acabamos de inserir) — openai_service injeta como user separado
    if history:
        history = history[:-len(messages)] if len(messages) <= len(history) else []

    # "Digitando..."
    try:
        await uazapi.send_presence(phone, "composing")
    except Exception:
        pass

    try:
        reply, tools_executed = await openai_service.chat(lead, user_text, history)
    except Exception as e:
        logger.exception(f"openai chat falhou: {e}")
        reply = "Tive um soluço aqui agora, pode repetir, por favor?"
        tools_executed = []

    # Persiste tool calls
    for t in tools_executed:
        await contact_updater.save_message(
            lead.id,
            "tool",
            content=str(t.get("result", {})),
            tool_name=t["name"],
            tool_args=t.get("args"),
        )

    if reply:
        await contact_updater.save_message(lead.id, "assistant", reply)
        await uazapi.send_text(phone, reply, delay=1500)
