"""
Follow-up automático de leads inativos.

Regras:
- FU1 disparado 2h após última msg do CLIENTE
- FU2 disparado 8h após última msg do CLIENTE
- FU3 disparado 24h após última msg do CLIENTE
- Se cliente responder qualquer coisa entre tentativas, contador reseta
- Só considera leads onde a última msg do cliente foi nos últimos 7 dias
  (não spammar histórico antigo esquecido)
- Após FU3, lead marcado como 'follow_up_esgotado', não recebe mais
- Só dispara pra leads com IA ligada e status não terminal
- Mensagem gerada pela LLM com base no histórico da conversa
"""

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Any
from loguru import logger
from openai import AsyncOpenAI
from sqlalchemy import select, text

from app.core.config import get_settings
from app.core.db import AsyncSessionLocal
from app.models import Lead, Message
from app.services import contact_updater
from app.services.uazapi import uazapi

settings = get_settings()
_openai = AsyncOpenAI(api_key=settings.openai_api_key)


# Intervalos a partir da última msg do cliente
FU_INTERVALS = {
    1: timedelta(hours=2),
    2: timedelta(hours=8),
    3: timedelta(hours=24),
}
# Só considera leads onde a última msg do cliente foi nesse intervalo.
# Protege contra "reengajar" leads que sumiram há muito tempo (frios demais).
MAX_AGE = timedelta(hours=48)

# Cooldown mínimo entre follow-ups do MESMO lead.
# Se o cliente ficou muito tempo inativo (ex: 30h) e nunca recebeu FU,
# sem cooldown ele receberia FU1+FU2+FU3 em ~45min (uma por batch).
# Com cooldown, garante espaçamento mínimo entre tentativas.
MIN_COOLDOWN = timedelta(hours=2)

TERMINAL_STATUSES = ("transferido_para_time", "atendimento_humano", "follow_up_esgotado")


async def find_candidates() -> list[dict[str, Any]]:
    """Retorna leads que precisam de follow-up agora."""
    now = datetime.now(timezone.utc)
    query = text("""
    SELECT
      l.id::text as lead_id,
      l.telefone,
      l.push_name,
      l.full_name,
      (SELECT max(m.created_at) FROM messages m
       WHERE m.lead_id = l.id AND m.role = 'user') as last_user_msg,
      (SELECT count(*) FROM follow_ups f
       WHERE f.lead_id = l.id
         AND f.enviado_em > COALESCE(
           (SELECT max(m2.created_at) FROM messages m2
            WHERE m2.lead_id = l.id AND m2.role = 'user'),
           '1970-01-01'::timestamptz
         )) as fu_since_last_msg,
      (SELECT max(f.enviado_em) FROM follow_ups f
       WHERE f.lead_id = l.id) as last_fu_at
    FROM leads l
    WHERE l.ia_on_off = 'ON'
      AND (l.status_funil_vendas IS NULL
           OR l.status_funil_vendas NOT IN ('transferido_para_time','atendimento_humano','follow_up_esgotado'))
    """)
    async with AsyncSessionLocal() as session:
        result = await session.execute(query)
        rows = result.mappings().all()

    candidates = []
    for row in rows:
        last_user = row["last_user_msg"]
        fu_count = row["fu_since_last_msg"] or 0
        last_fu_at = row["last_fu_at"]

        if last_user is None:
            continue
        if fu_count >= 3:
            continue
        elapsed = now - last_user
        if elapsed > MAX_AGE:
            continue

        # Cooldown: se enviou FU recentemente, espera antes do próximo
        if last_fu_at is not None and (now - last_fu_at) < MIN_COOLDOWN:
            continue

        next_fu = fu_count + 1
        if elapsed >= FU_INTERVALS[next_fu]:
            candidates.append({
                "lead_id": row["lead_id"],
                "telefone": row["telefone"],
                "push_name": row["push_name"],
                "full_name": row["full_name"],
                "tentativa": next_fu,
                "last_user_msg": last_user,
                "elapsed_hours": round(elapsed.total_seconds() / 3600, 1),
            })
    return candidates


async def _generate_message(lead: Lead, history: list[Message], tentativa: int) -> str:
    """Gera mensagem contextual pra follow-up usando LLM."""
    convo = "\n".join(
        f"{m.role.upper()}: {m.content[:400]}"
        for m in history[-15:]
    )
    nome = None
    if lead.full_name:
        nome = lead.full_name.split()[0]
    elif lead.push_name:
        first = lead.push_name.strip().split()[0]
        # rejeita se for lixo (dígito, muito curto)
        if len(first) >= 2 and not any(c.isdigit() for c in first):
            nome = first

    tom_map = {
        1: "acolhedor e casual, como um lembrete leve. Ex: 'Oi, ainda por aí?'",
        2: "ainda simpático mas mais direto. Mostra disponibilidade sem pressão.",
        3: "última tentativa amistosa. Sem cobrança, deixa a porta aberta.",
    }
    tom = tom_map[tentativa]

    system = (
        "Você é a Lara, atendente de energia solar da FP Solar. "
        f"Vai gerar uma mensagem de FOLLOW-UP (tentativa #{tentativa} de 3) "
        f"pra um cliente que parou de responder. "
        f"Tom desta tentativa: {tom} "
        "Seja MUITO BREVE (1 a 3 frases curtas). "
        "Retome exatamente de onde ele parou, sem recomeçar do zero. "
        f"Se souber o nome ({nome or 'desconhecido'}), use-o no início. "
        "Termina com uma pergunta curta ou convite leve pra continuar. "
        "No máximo 1 emoji. Português BR natural, sem formalidade excessiva. "
        "Não invente informações que o cliente não deu."
    )

    user_prompt = (
        f"Conversa até aqui:\n\n{convo}\n\n"
        "Escreva APENAS a mensagem que a Lara enviaria agora ao cliente. "
        "Sem prefixo, sem explicação, só o texto puro."
    )

    resp = await _openai.chat.completions.create(
        model=settings.openai_model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.6,
        max_tokens=200,
    )
    return (resp.choices[0].message.content or "").strip()


async def _send_and_record(candidate: dict) -> dict:
    """Envia um follow-up e registra em follow_ups + messages."""
    import uuid as _uuid
    lead_id = _uuid.UUID(candidate["lead_id"])
    phone = candidate["telefone"]
    tentativa = candidate["tentativa"]

    async with AsyncSessionLocal() as session:
        lead = (await session.execute(select(Lead).where(Lead.id == lead_id))).scalar_one()
        history = await contact_updater.load_history(lead.id, limit=30)

    try:
        message = await _generate_message(lead, history, tentativa)
    except Exception as e:
        logger.exception(f"[followup] LLM falhou phone={phone}: {e}")
        return {"ok": False, "phone": phone, "erro": f"llm: {e}"}

    if not message:
        return {"ok": False, "phone": phone, "erro": "mensagem vazia"}

    result = await uazapi.send_text(phone, message, delay=1500)
    sucesso = result is not None
    wpp_id = result.get("messageid") if result else None

    async with AsyncSessionLocal() as session:
        await session.execute(text("""
          INSERT INTO follow_ups (lead_id, tentativa, mensagem, sucesso, erro, message_id_wpp)
          VALUES (:lead_id, :tentativa, :mensagem, :sucesso, :erro, :wpp_id)
        """), {
            "lead_id": lead_id,
            "tentativa": tentativa,
            "mensagem": message,
            "sucesso": sucesso,
            "erro": None if sucesso else "uazapi retornou None",
            "wpp_id": wpp_id,
        })
        # Grava em messages tb pra a Lara ver no próximo turno
        msg = Message(
            lead_id=lead.id, role="assistant", content=message,
            message_id_wpp=wpp_id, message_type="text",
        )
        session.add(msg)
        # FU3 bem-sucedido → marca como esgotado
        if tentativa == 3 and sucesso:
            db_lead = (await session.execute(select(Lead).where(Lead.id == lead_id))).scalar_one()
            db_lead.status_funil_vendas = "follow_up_esgotado"
            db_lead.etapa_follow_up = "follow_up_esgotado"
        await session.commit()

    logger.info(f"[followup] FU{tentativa} phone={phone} ok={sucesso} msg={message[:80]!r}")
    return {"ok": sucesso, "phone": phone, "tentativa": tentativa, "mensagem": message}


async def run_batch() -> dict:
    """Executa uma sweep de follow-ups. Chamado pelo endpoint /admin e pelo scheduler."""
    try:
        candidates = await find_candidates()
    except Exception as e:
        logger.exception(f"[followup] find_candidates falhou: {e}")
        return {"candidatos": 0, "enviados_ok": 0, "erro": str(e)}

    if not candidates:
        return {"candidatos": 0, "enviados_ok": 0, "resultados": []}

    logger.info(f"[followup] batch: {len(candidates)} candidatos")
    results = []
    for cand in candidates:
        try:
            r = await _send_and_record(cand)
            results.append(r)
        except Exception as e:
            logger.exception(f"[followup] erro enviando {cand.get('telefone')}: {e}")
            results.append({"ok": False, "phone": cand.get("telefone"), "erro": str(e)})
        await asyncio.sleep(1)  # pequeno delay entre envios pra não estressar uazapi
    ok = sum(1 for r in results if r.get("ok"))
    logger.info(f"[followup] batch fim: {ok}/{len(candidates)} enviados")
    return {"candidatos": len(candidates), "enviados_ok": ok, "resultados": results}
