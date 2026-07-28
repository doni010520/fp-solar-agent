import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from loguru import logger

from app.api import dashboard, health, webhook
from app.core.config import get_settings
from app.core.logging import setup_logging


async def _followup_loop() -> None:
    """Roda follow-ups a cada 15 minutos. Se falhar, loga e continua."""
    from app.services import follow_up_service
    # Espera 60s no boot pra deixar o app estabilizar antes da 1a rodada
    await asyncio.sleep(60)
    while True:
        try:
            result = await follow_up_service.run_batch()
            if result.get("candidatos", 0) > 0:
                logger.info(f"[followup-loop] {result['enviados_ok']}/{result['candidatos']} enviados")
        except Exception as e:
            logger.exception(f"[followup-loop] erro: {e}")
        await asyncio.sleep(900)  # 15 minutos


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    settings = get_settings()
    logger.info(f"🚀 FP Solar Agent — env={settings.app_env} model={settings.openai_model}")
    followup_task = asyncio.create_task(_followup_loop())
    try:
        yield
    finally:
        followup_task.cancel()
        logger.info("👋 shutdown")


app = FastAPI(title="FP Solar Agent — Lara", version="0.1.0", lifespan=lifespan)
app.include_router(health.router)
app.include_router(webhook.router)
app.include_router(dashboard.router)
