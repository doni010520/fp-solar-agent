"""
Processa mídia recebida no WhatsApp e devolve representação textual para o LLM.

- audio → transcrição via Whisper (uazapi já tem builtin) com fallback OpenAI
- image → descrição via gpt-4o vision (URL temporária do uazapi)
- document (pdf) → extração de texto via pypdf
"""

from io import BytesIO
import httpx
from loguru import logger
from openai import AsyncOpenAI
from pypdf import PdfReader

from app.core.config import get_settings
from app.services.uazapi import uazapi

settings = get_settings()
_openai = AsyncOpenAI(api_key=settings.openai_api_key)


async def process_audio(message_id: str) -> str:
    """Tenta transcrição embutida do uazapi; se falhar, baixa e usa Whisper OpenAI."""
    transcription = await uazapi.transcribe_audio(message_id)
    if transcription:
        return f"[áudio transcrito]: {transcription}"

    logger.warning(f"uazapi transcribe falhou para {message_id}; tentando OpenAI Whisper")
    url = await uazapi.get_media_url(message_id)
    if not url:
        return "[áudio recebido, mas não foi possível transcrever]"

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.get(url)
            r.raise_for_status()
            audio_bytes = r.content
        result = await _openai.audio.transcriptions.create(
            model=settings.openai_transcribe_model,
            file=("audio.ogg", audio_bytes, "audio/ogg"),
        )
        return f"[áudio transcrito]: {result.text}"
    except Exception as e:
        logger.error(f"OpenAI Whisper falhou: {e}")
        return "[áudio recebido, mas não foi possível transcrever]"


async def process_image(message_id: str, caption: str = "") -> str:
    """Descreve a imagem usando gpt-4o vision a partir da URL pública do uazapi."""
    url = await uazapi.get_media_url(message_id)
    if not url:
        return "[imagem recebida, mas não foi possível acessar]"

    try:
        prompt = (
            "Descreva objetivamente o conteúdo desta imagem em português. "
            "Se for uma conta de luz, extraia: titular, endereço, valor total, "
            "consumo em kWh, mês de referência. Se for foto de telhado, descreva o tipo. "
            "Seja conciso."
        )
        if caption:
            prompt += f"\n\nLegenda enviada pelo cliente: {caption}"

        resp = await _openai.chat.completions.create(
            model=settings.openai_vision_model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": url}},
                    ],
                }
            ],
            max_tokens=400,
        )
        return f"[imagem]: {resp.choices[0].message.content}"
    except Exception as e:
        logger.error(f"Vision falhou: {e}")
        return "[imagem recebida, mas não foi possível analisar]"


async def process_document(message_id: str, caption: str = "") -> str:
    """Baixa e extrai texto de PDF. Outros tipos retornam placeholder."""
    url = await uazapi.get_media_url(message_id)
    if not url:
        return "[documento recebido, mas não foi possível baixar]"

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.get(url)
            r.raise_for_status()
            data = r.content

        if not (data[:4] == b"%PDF"):
            return f"[documento não-PDF recebido: {caption or 'sem legenda'}]"

        reader = PdfReader(BytesIO(data))
        pages = []
        for i, page in enumerate(reader.pages[:10]):  # limita 10 páginas
            try:
                pages.append(page.extract_text() or "")
            except Exception:
                pages.append("")
        text = "\n".join(pages).strip()
        if not text:
            return "[PDF recebido, mas sem texto extraível (possivelmente imagem)]"
        return f"[PDF — conteúdo extraído]:\n{text[:4000]}"
    except Exception as e:
        logger.error(f"PDF extract falhou: {e}")
        return "[documento recebido, mas não foi possível processar]"


async def process_media(message_type: str, message_id: str, caption: str = "") -> str:
    """Roteador. Retorna string textual pra alimentar o LLM."""
    if message_type == "audio":
        return await process_audio(message_id)
    if message_type == "image":
        return await process_image(message_id, caption)
    if message_type == "document":
        return await process_document(message_id, caption)
    return f"[{message_type} recebido — tipo não suportado]"
