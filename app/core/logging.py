import sys
from loguru import logger
from app.core.config import get_settings


def setup_logging() -> None:
    settings = get_settings()
    # Garante UTF-8 no stdout (Windows tem console cp1252 que quebra emojis)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    logger.remove()
    logger.add(
        sys.stdout,
        level=settings.log_level,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        colorize=True,
        enqueue=False,
    )
