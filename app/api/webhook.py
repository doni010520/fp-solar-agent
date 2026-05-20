from fastapi import APIRouter, Request, HTTPException
from loguru import logger

from app.core.config import get_settings
from app.services.uazapi import uazapi
from app.services.conversation import handle_incoming

settings = get_settings()
router = APIRouter()


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
        await handle_incoming(parsed)
    except Exception as e:
        logger.exception(f"handle_incoming falhou: {e}")
        return {"ok": False, "error": str(e)}

    return {"ok": True}
