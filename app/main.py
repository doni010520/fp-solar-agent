from contextlib import asynccontextmanager
from fastapi import FastAPI
from loguru import logger

from app.api import health, webhook
from app.core.config import get_settings
from app.core.logging import setup_logging


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    settings = get_settings()
    logger.info(f"🚀 FP Solar Agent — env={settings.app_env} model={settings.openai_model}")
    yield
    logger.info("👋 shutdown")


app = FastAPI(title="FP Solar Agent — Lara", version="0.1.0", lifespan=lifespan)
app.include_router(health.router)
app.include_router(webhook.router)
