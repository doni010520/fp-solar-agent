"""Buffer circular em memória dos últimos N logs, exposto via /admin/logs."""
from collections import deque
import sys
from loguru import logger

_RING: deque[str] = deque(maxlen=2000)


def _sink(message):
    _RING.append(message.rstrip())


def install() -> None:
    """Chamado uma vez no setup_logging. Adiciona um sink que captura
    todos os logs num ring buffer pra leitura via API."""
    logger.add(_sink, level="DEBUG", format="{time:HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}")


def tail(n: int = 200, contains: str | None = None) -> list[str]:
    lines = list(_RING)
    if contains:
        lines = [l for l in lines if contains.lower() in l.lower()]
    return lines[-n:]
