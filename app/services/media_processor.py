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
    """Baixa o áudio do uazapi e transcreve via Whisper OpenAI (language=pt).

    Estratégia: NÃO usa a transcrição embutida do uazapi (Whisper sem hint
    de idioma estava interpretando PT-BR como inglês). Baixamos o áudio e
    chamamos a API do Whisper diretamente passando language='pt'.
    """
    logger.info(f"[audio] iniciando transcrição msgid={message_id}")

    # Baixa o áudio (preferindo URL pra economizar memória; fallback base64)
    url = None
    audio_bytes: bytes | None = None
    mimetype = "audio/ogg"
    try:
        result = await uazapi.download_media(
            message_id=message_id,
            return_link=True,
            return_base64=False,
            transcribe=False,
            generate_mp3=False,  # OGG é o formato nativo do WhatsApp
        )
        if result:
            url = result.get("fileURL")
            mimetype = result.get("mimetype") or mimetype
    except Exception as e:
        logger.error(f"[audio] download_media raised: {type(e).__name__}: {e}")

    if not url:
        # Tenta direto em base64 como fallback
        try:
            result = await uazapi.download_media(
                message_id=message_id, return_base64=True, return_link=False
            )
            if result and result.get("base64Data"):
                import base64
                audio_bytes = base64.b64decode(result["base64Data"])
                mimetype = result.get("mimetype") or mimetype
                logger.info(f"[audio] obtido via base64 ({len(audio_bytes)} bytes)")
        except Exception as e:
            logger.error(f"[audio] base64 fallback raised: {type(e).__name__}: {e}")

    if url and not audio_bytes:
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                r = await client.get(url)
                r.raise_for_status()
                audio_bytes = r.content
            logger.info(f"[audio] baixou {len(audio_bytes)} bytes da URL")
        except Exception as e:
            logger.error(f"[audio] download from URL falhou: {type(e).__name__}: {e}")

    if not audio_bytes:
        return "[áudio recebido, mas não foi possível baixar]"

    # Escolhe extensão coerente com o mimetype pra Whisper aceitar
    ext = "ogg"
    if "mp3" in mimetype or "mpeg" in mimetype:
        ext = "mp3"
    elif "wav" in mimetype:
        ext = "wav"
    elif "m4a" in mimetype or "mp4" in mimetype:
        ext = "m4a"

    try:
        result = await _openai.audio.transcriptions.create(
            model=settings.openai_transcribe_model,
            file=(f"audio.{ext}", audio_bytes, mimetype),
            language="pt",
        )
        text = (result.text or "").strip()
        logger.info(f"[audio] Whisper OK ({len(text)} chars) text={text[:120]!r}")
        if not text:
            return "[áudio recebido, mas a transcrição veio vazia]"
        return f"[áudio transcrito]: {text}"
    except Exception as e:
        logger.error(f"[audio] Whisper falhou: {type(e).__name__}: {e}")
        return "[áudio recebido, mas não foi possível transcrever]"


async def process_image(message_id: str, caption: str = "") -> str:
    """Descreve a imagem usando gpt-4o vision com foco em conta de luz e telhado."""
    url = await uazapi.get_media_url(message_id)
    if not url:
        return "[imagem recebida, mas não foi possível acessar]"

    try:
        prompt = (
            "Analise esta imagem enviada por um cliente que está pedindo orçamento de energia solar. "
            "Classifique em uma destas categorias e responda em português:\n\n"
            "1. **CONTA DE LUZ** — extraia os seguintes dados, marcando 'não visível' quando faltar:\n"
            "   - Titular: nome\n"
            "   - Endereço (cidade/UF)\n"
            "   - Concessionária (Coelba, Equatorial, Cemig, etc.)\n"
            "   - Mês de referência\n"
            "   - Valor total da fatura (R$)\n"
            "   - Consumo do mês (kWh)\n"
            "   - Tipo de ligação (Monofásica / Bifásica / Trifásica)\n"
            "   - **Histórico de consumo dos últimos 12 meses (kWh)** — se houver gráfico ou tabela, "
            "liste mês a mês. Esse dado é CRÍTICO. Se não estiver visível, diga 'gráfico de 12 meses NÃO VISÍVEL'.\n"
            "   - Média mensal estimada (kWh)\n\n"
            "2. **FOTO DE TELHADO** — descreva: tipo (colonial barro / zinco / eternit / laje), "
            "estado de conservação, área aproximada se der pra estimar, orientação solar se for visível.\n\n"
            "3. **OUTRO** — descreva em uma linha o que é.\n\n"
            "Comece sua resposta com o prefixo `CATEGORIA: <nome>` e depois os dados. Seja objetiva."
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
                        {"type": "image_url", "image_url": {"url": url, "detail": "high"}},
                    ],
                }
            ],
            max_tokens=700,
        )
        analysis = resp.choices[0].message.content or ""
        # Prefixo dinâmico: se for conta de luz, marca explicitamente pra Lara
        first_line = analysis.strip().splitlines()[0] if analysis else ""
        if "CONTA DE LUZ" in first_line.upper():
            return f"[conta de luz]:\n{analysis}"
        if "TELHADO" in first_line.upper():
            return f"[foto de telhado]:\n{analysis}"
        return f"[imagem]:\n{analysis}"
    except Exception as e:
        logger.error(f"Vision falhou: {e}")
        return "[imagem recebida, mas não foi possível analisar]"


async def process_document(message_id: str, caption: str = "") -> str:
    """Baixa e extrai texto de PDF. Outros tipos retornam placeholder."""
    logger.info(f"[doc] iniciando msgid={message_id}")
    try:
        url = await uazapi.get_media_url(message_id)
    except Exception as e:
        logger.error(f"[doc] get_media_url raised: {type(e).__name__}: {e}")
        url = None
    if not url:
        logger.warning(f"[doc] sem URL pra msgid={message_id}")
        return "[documento recebido, mas não foi possível baixar]"
    logger.info(f"[doc] URL obtida, baixando…")

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.get(url)
            r.raise_for_status()
            data = r.content
        logger.info(f"[doc] baixou {len(data)} bytes")

        if not (data[:4] == b"%PDF"):
            logger.info(f"[doc] não é PDF (primeiros bytes: {data[:8]!r})")
            return f"[documento não-PDF recebido: {caption or 'sem legenda'}]"

        reader = PdfReader(BytesIO(data))
        pages = []
        for i, page in enumerate(reader.pages[:10]):
            try:
                pages.append(page.extract_text() or "")
            except Exception:
                pages.append("")
        text = "\n".join(pages).strip()
        logger.info(f"[doc] PDF extraído: {len(reader.pages)} pgs, {len(text)} chars de texto")
        if not text:
            return "[PDF recebido, mas sem texto extraível (possivelmente imagem)]"
        return f"[PDF — conteúdo extraído]:\n{text[:4000]}"
    except Exception as e:
        logger.error(f"[doc] PDF extract falhou: {type(e).__name__}: {e}")
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
