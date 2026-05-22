"""
Orquestrador da conversa. Cola buffer → mídia → openai → uazapi → persistência.

Fluxo (cada mensagem do cliente):
1. Webhook entrega evento ao FastAPI → handle_incoming()
2. handle_incoming() resolve mídia (transcrição/descrição/PDF) e adiciona ao buffer
3. Após N segundos de silêncio, buffer chama _flush()
4. _flush() carrega histórico, monta texto unificado, chama Lara (openai_service)
5. Persiste mensagens e tools executadas, envia resposta via uazapi
"""

import unicodedata
from loguru import logger

from app.services import contact_updater, openai_service, admin_commands
from app.services.buffer import PendingMessage, buffer
from app.services.media_processor import process_media
from app.core.config import get_settings
from app.services.uazapi import uazapi

settings = get_settings()


# ── Detecção de gatilho de atendimento humano ─────────────
# Atendente envia uma frase tipo "Olá! Equipe FP Solar aqui" no chat do cliente.
# A msg vai pro cliente normalmente E desliga a IA.
# Variações aceitas: ignora acento, case, espaços extras e pontuação.
_HUMAN_TRIGGERS = [
    "equipe fp solar",      # "Olá! Equipe FP Solar aqui", "Equipe FP Solar falando"
    "fp solar aqui",        # "FP Solar aqui!", "Aqui é a FP Solar"
    "aqui e a fp solar",    # "Olá, aqui é a FP Solar"
    "aqui da fp solar",     # "Pedro aqui da FP Solar"
    "atendente fp solar",   # "Atendente FP Solar respondendo"
    "time fp solar",        # "Time FP Solar aqui"
]


def _normalize(text: str) -> str:
    """Remove acentos, baixa caixa, normaliza espaços."""
    nfkd = unicodedata.normalize("NFKD", text)
    no_accent = "".join(c for c in nfkd if not unicodedata.combining(c))
    return " ".join(no_accent.lower().split())


def _contains_human_trigger(text: str) -> bool:
    norm = _normalize(text)
    return any(trig in norm for trig in _HUMAN_TRIGGERS)


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

    # ── Detecção de tomada de atendimento por humano ──────────
    # fromMe=true significa que a mensagem partiu da própria conta da FP Solar.
    # Pode ser: (a) eco da própria Lara via API; (b) humano respondendo via app/web.
    # Diferenciamos pelo messageid: se está no nosso DB, fomos nós; senão, é humano.
    if parsed.get("from_me"):
        message_id = parsed.get("message_id", "")
        logger.info(f"[conv] fromMe phone={phone} msgid={message_id} body={(body or '')[:80]!r}")

        # ── Gatilho explícito de atendimento humano ───────────
        # Frase que o atendente digita pro cliente assumindo a conversa.
        # Robusto: case-insensitive, ignora acentos, busca substring.
        if body and _contains_human_trigger(body):
            lead_existing = await contact_updater.get_or_create_lead(phone, push_name)
            if lead_existing.ia_on_off == "ON":
                await contact_updater.disable_ia(phone, "atendimento_humano")
                logger.info(f"[conv] gatilho FP Solar detectado phone={phone}, IA OFF")
            return

        # Cache em memória dos messageids que ENVIAMOS via API.
        if uazapi.is_outbound(message_id):
            return
        # Fallback: olha no DB (caso o cache em memória tenha sido perdido em restart)
        if message_id and await contact_updater.is_our_outbound_message(message_id):
            return
        # Não é nosso → humano assumiu o WhatsApp
        lead_existing = await contact_updater.get_or_create_lead(phone, push_name)
        if lead_existing.ia_on_off == "ON":
            await contact_updater.disable_ia(phone, "atendimento_humano")
            logger.info(f"[conv] humano assumiu phone={phone}, IA OFF automático")
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
            # Salva a resposta do comando como assistant + grava o wpp_id
            # pra não confundir com humano no eco do webhook.
            saved = await contact_updater.save_message(lead.id, "assistant", reply)
            result = await uazapi.send_text(phone, reply, delay=500)
            if result and result.get("messageid"):
                await contact_updater.set_message_wpp_id(saved.id, result["messageid"])
            return

    if lead.ia_on_off == "OFF":
        logger.info(f"[conv] IA OFF para {phone}, ignorando mensagem")
        return

    await contact_updater.touch_last_contact(phone)

    # Tipos descartáveis (não devem virar conversa)
    DISCARD = {"sticker", "reaction", "location", "contact", "poll", "poll_update", "view_once"}

    if msg_type in ("audio", "image", "document"):
        logger.info(f"[conv] mídia tipo={msg_type} msgid={message_id} caption={(body or '')[:60]!r}")
        try:
            body = await process_media(msg_type, message_id, caption=body)
            logger.info(f"[conv] mídia processada body_len={len(body)} prefix={(body or '')[:80]!r}")
        except Exception as e:
            logger.exception(f"media_processor falhou: {e}")
            body = f"[{msg_type} recebido]"
    elif msg_type in DISCARD:
        logger.info(f"[conv] tipo descartado: {msg_type}")
        return
    else:
        # Qualquer outro tipo (text, extendedTextMessage, tipos desconhecidos)
        # é tratado como texto se houver body. Se body vazio, ignora.
        if msg_type != "text":
            logger.info(f"[conv] tipo {msg_type!r} tratado como texto (body={len(body)} chars)")

    if not body:
        logger.info(f"[conv] body vazio para {phone} (msg_type={msg_type}), ignorando")
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
        saved = await contact_updater.save_message(lead.id, "assistant", reply)
        result = await uazapi.send_text(phone, reply, delay=1500)
        # Grava o messageid retornado pelo uazapi pra reconhecer eco no webhook
        # (e diferenciar de tomada por humano).
        if result and result.get("messageid"):
            await contact_updater.set_message_wpp_id(saved.id, result["messageid"])
