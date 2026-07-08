from fastapi import APIRouter
from sqlalchemy import text
from app.core.db import engine
from app.services.uazapi import uazapi

router = APIRouter()


@router.get("/live")
async def live() -> dict:
    """Liveness barato — só confirma que o processo está respondendo.
    NÃO toca em DB nem em rede externa. Usado pelo Docker HEALTHCHECK
    pra evitar restart loops causados por blip do uazapi ou DB."""
    return {"status": "ok"}


@router.get("/health")
async def health() -> dict:
    checks: dict = {"app": "ok"}
    try:
        async with engine.connect() as c:
            await c.execute(text("select 1"))
        checks["db"] = "ok"
    except Exception as e:
        checks["db"] = f"error: {e}"

    try:
        status = await uazapi.get_instance_status()
        checks["uazapi"] = "ok" if status else "unreachable"
    except Exception as e:
        checks["uazapi"] = f"error: {e}"

    overall = all(v == "ok" for v in checks.values())
    return {"status": "ok" if overall else "degraded", "checks": checks}
