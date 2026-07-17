import asyncio
from fastapi import APIRouter, Request, HTTPException
from loguru import logger

from app.core.config import get_settings
from app.services.uazapi import uazapi
from app.services.conversation import handle_incoming

settings = get_settings()
router = APIRouter()


async def _handle_with_retry(parsed: dict, max_attempts: int = 3) -> None:
    """Tenta processar o webhook até 3x com backoff. Cobre falhas transientes
    de conexão com o Supabase pooler (asyncpg timeout, connection refused)."""
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            await handle_incoming(parsed)
            if attempt > 1:
                logger.info(f"handle_incoming ok na tentativa {attempt} phone={parsed.get('phone')}")
            return
        except Exception as e:
            last_exc = e
            logger.warning(f"handle_incoming falhou tentativa {attempt}/{max_attempts} phone={parsed.get('phone')}: {type(e).__name__}: {str(e)[:200]}")
            if attempt < max_attempts:
                # backoff exponencial: 1s, 3s
                await asyncio.sleep(attempt * 2)
    # Todas as tentativas falharam
    logger.exception(f"handle_incoming EXAURIDO após {max_attempts} tentativas phone={parsed.get('phone')}: {last_exc}")
    raise last_exc  # deixa o wrapper externo decidir o que fazer


@router.post("/webhook/uazapi")
async def uazapi_webhook(request: Request) -> dict:
    # Validação opcional: header "x-webhook-secret"
    if settings.uazapi_webhook_secret:
        if request.headers.get("x-webhook-secret") != settings.uazapi_webhook_secret:
            raise HTTPException(status_code=401, detail="invalid webhook secret")

    try:
        payload = await request.json()
    except Exception as e:
        logger.warning(f"webhook payload inválido: {e}")
        return {"ok": False, "error": "invalid_json"}

    parsed = uazapi.parse_webhook(payload)
    if not parsed:
        # Loga o messageType original quando não conseguimos parsear (debug)
        msg = payload.get("message") or payload.get("data") or {}
        mtype = msg.get("messageType") or msg.get("type")
        from_me = msg.get("fromMe")
        is_group = msg.get("isGroup")
        logger.info(f"webhook skipped: event={payload.get('EventType') or payload.get('event')} type={mtype} fromMe={from_me} isGroup={is_group}")
        return {"ok": True, "skipped": True}

    logger.info(
        f"webhook parsed: phone={parsed['phone']} type={parsed['type']} "
        f"(raw={parsed.get('type_raw')}) body_len={len(parsed.get('body',''))}"
    )

    try:
        await _handle_with_retry(parsed)
    except Exception as e:
        return {"ok": False, "error": str(e)}

    return {"ok": True}
