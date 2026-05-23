"""
Processa mídia recebida no WhatsApp e devolve representação textual para o LLM.

- audio → download + Whisper OpenAI com language=pt
- image → gpt-4o vision (classifica conta de luz / telhado / outro)
- document (pdf):
    1. pypdf pra texto direto (PDFs digitais)
    2. fallback: renderiza páginas como imagem via pypdfium2 e usa
       gpt-4o vision com o mesmo prompt de conta de luz
"""

import base64
from io import BytesIO
import httpx
from loguru import logger
from openai import AsyncOpenAI
from pypdf import PdfReader
import pypdfium2 as pdfium

from app.core.config import get_settings
from app.services.uazapi import uazapi

settings = get_settings()
_openai = AsyncOpenAI(api_key=settings.openai_api_key)


_BILL_PROMPT = (
    "Analise esta imagem (página de conta de luz ou documento similar) enviada por um "
    "cliente que está pedindo orçamento de energia solar. Extraia em português:\n\n"
    "- Titular (nome)\n"
    "- Endereço (cidade/UF)\n"
    "- Concessionária (Coelba, Equatorial, Cemig, etc.)\n"
    "- Mês de referência\n"
    "- Valor total da fatura (R$)\n"
    "- Consumo do mês (kWh)\n"
    "- Tipo de ligação (Monofásica / Bifásica / Trifásica)\n"
    "- **Histórico de consumo dos últimos 12 meses (kWh)** — se houver gráfico ou tabela, "
    "liste mês a mês. Esse dado é CRÍTICO.\n"
    "- Média mensal estimada (kWh)\n\n"
    "Quando um dado não estiver visível, escreva 'não visível'. Seja objetiva."
)


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


_VISION_SYSTEM = (
    "Você é um assistente especializado em ler contas de luz e fotos de telhado "
    "para orientar a equipe técnica de uma empresa de energia solar (FP Solar). "
    "O cliente é maior de idade, enviou a imagem voluntariamente e consentiu o "
    "uso dos dados pra elaboração de orçamento (em conformidade com LGPD). "
    "Sua tarefa é APENAS extrair os campos pedidos no prompt. Não recusa imagens "
    "que mostrem CPF, endereço ou dados de identificação do titular — esses dados "
    "são públicos no documento e necessários pra qualificação. Se a imagem for "
    "ilegível, diga 'imagem ilegível' e nada mais."
)


_VISION_PROMPT = (
    "Analise esta imagem enviada pelo cliente. Classifique em UMA categoria e "
    "extraia os campos em português:\n\n"
    "1. **CONTA DE LUZ** (fatura de energia elétrica):\n"
    "   - Titular: nome completo\n"
    "   - Endereço (cidade/UF se visível)\n"
    "   - Concessionária (Coelba, Equatorial, Cemig, Enel, etc.)\n"
    "   - Mês de referência\n"
    "   - Valor total da fatura (R$)\n"
    "   - Consumo do mês (kWh)\n"
    "   - Tipo de ligação (Monofásica / Bifásica / Trifásica)\n"
    "   - **Histórico de consumo dos últimos 12 meses (kWh)** — extraia do gráfico/tabela mês a mês. CRÍTICO.\n"
    "   - Média mensal estimada (kWh)\n\n"
    "2. **FOTO DE TELHADO**: tipo (colonial barro / zinco / eternit / laje), "
    "estado de conservação, área estimada (m²), orientação se visível.\n\n"
    "3. **OUTRO**: descreva em 1 linha o que é.\n\n"
    "Quando um dado não estiver visível, escreva 'não visível'. NÃO invente. "
    "Comece com `CATEGORIA: <nome>` e depois os campos. Seja objetiva."
)


_REFUSAL_MARKERS = (
    "i'm sorry, i can't",
    "i cannot assist",
    "i can't assist",
    "desculpe, não posso",
    "não posso ajudar",
    "i'm unable to",
)


async def process_image(message_id: str, caption: str = "") -> str:
    """Descreve a imagem usando gpt-4o vision com foco em conta de luz e telhado."""
    url = await uazapi.get_media_url(message_id)
    if not url:
        return "[imagem recebida, mas não foi possível acessar]"

    user_text = _VISION_PROMPT + (f"\n\nLegenda enviada pelo cliente: {caption}" if caption else "")

    try:
        resp = await _openai.chat.completions.create(
            model=settings.openai_vision_model,
            messages=[
                {"role": "system", "content": _VISION_SYSTEM},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_text},
                        {"type": "image_url", "image_url": {"url": url, "detail": "high"}},
                    ],
                },
            ],
            max_tokens=700,
        )
        analysis = (resp.choices[0].message.content or "").strip()
        first_line = analysis.splitlines()[0] if analysis else ""
        lower = analysis.lower()

        # Detecta recusa do modelo (guardrail) e cai em fallback útil
        if not analysis or any(mk in lower for mk in _REFUSAL_MARKERS):
            logger.warning(f"[image] modelo recusou; analysis={analysis[:120]!r}")
            return (
                "[imagem recebida — o sistema automático não conseguiu ler. "
                "Peça os dados verbalmente: valor da conta, consumo em kWh, "
                "concessionária e histórico recente, ou tente outra foto.]"
            )

        if "CONTA DE LUZ" in first_line.upper():
            return f"[conta de luz]:\n{analysis}"
        if "TELHADO" in first_line.upper():
            return f"[foto de telhado]:\n{analysis}"
        return f"[imagem]:\n{analysis}"
    except Exception as e:
        logger.error(f"[image] vision falhou: {type(e).__name__}: {e}")
        return "[imagem recebida, mas não foi possível analisar]"


async def process_document(message_id: str, caption: str = "") -> str:
    """Baixa o documento. Se PDF, tenta pypdf primeiro; se vier vazio
    (PDF escaneado/imagem), renderiza páginas e chama gpt-4o vision."""
    logger.info(f"[doc] iniciando msgid={message_id}")
    try:
        url = await uazapi.get_media_url(message_id)
    except Exception as e:
        logger.error(f"[doc] get_media_url raised: {type(e).__name__}: {e}")
        url = None
    if not url:
        return "[documento recebido, mas não foi possível baixar]"

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.get(url)
            r.raise_for_status()
            data = r.content
        logger.info(f"[doc] baixou {len(data)} bytes")
    except Exception as e:
        logger.error(f"[doc] download URL falhou: {type(e).__name__}: {e}")
        return "[documento recebido, mas não foi possível baixar]"

    if data[:4] != b"%PDF":
        logger.info(f"[doc] não é PDF (primeiros bytes: {data[:8]!r})")
        return f"[documento não-PDF recebido: {caption or 'sem legenda'}]"

    # 1) Tenta extração de texto direto (PDFs digitais)
    text = ""
    n_pages = 0
    try:
        reader = PdfReader(BytesIO(data))
        n_pages = len(reader.pages)
        pages = []
        for page in reader.pages[:10]:
            try:
                pages.append(page.extract_text() or "")
            except Exception:
                pages.append("")
        text = "\n".join(pages).strip()
        logger.info(f"[doc] pypdf: {n_pages} pgs, {len(text)} chars")
    except Exception as e:
        logger.error(f"[doc] pypdf falhou: {type(e).__name__}: {e}")

    # Se texto é razoável (>120 chars), retorna direto
    if len(text) >= 120:
        return f"[PDF — conteúdo extraído]:\n{text[:6000]}"

    # 2) PDF é imagem (ou só com pouco texto) → renderiza com pypdfium2 + vision
    logger.info(f"[doc] PDF sem texto suficiente, usando vision…")
    try:
        analysis = await _pdf_to_vision_summary(data)
        logger.info(f"[doc] vision OK ({len(analysis)} chars)")
        return f"[conta de luz / PDF — análise por imagem]:\n{analysis}"
    except Exception as e:
        logger.exception(f"[doc] vision PDF falhou: {type(e).__name__}: {e}")
        return "[PDF recebido, mas não foi possível extrair o conteúdo]"


async def _pdf_to_vision_summary(pdf_bytes: bytes, max_pages: int = 4) -> str:
    """Renderiza as primeiras max_pages do PDF em PNG e chama gpt-4o vision.
    Retorna análise textual consolidada."""
    pdf = pdfium.PdfDocument(pdf_bytes)
    n = min(len(pdf), max_pages)

    images_b64: list[str] = []
    for i in range(n):
        page = pdf[i]
        # scale=2 → ~200dpi, bom equilíbrio entre qualidade e tamanho
        pil = page.render(scale=2).to_pil()
        buf = BytesIO()
        pil.save(buf, format="PNG", optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        images_b64.append(b64)

    # Monta payload de vision: prompt único + N imagens
    content: list = [{"type": "text", "text": _BILL_PROMPT + f"\n\n(O PDF tem {len(pdf)} páginas; analisando as {n} primeiras.)"}]
    for b64 in images_b64:
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{b64}", "detail": "high"},
        })

    resp = await _openai.chat.completions.create(
        model=settings.openai_vision_model,
        messages=[{"role": "user", "content": content}],
        max_tokens=900,
    )
    return (resp.choices[0].message.content or "").strip()


async def process_media(message_type: str, message_id: str, caption: str = "") -> str:
    """Roteador. Retorna string textual pra alimentar o LLM."""
    if message_type == "audio":
        return await process_audio(message_id)
    if message_type == "image":
        return await process_image(message_id, caption)
    if message_type == "document":
        return await process_document(message_id, caption)
    return f"[{message_type} recebido — tipo não suportado]"
