"""
OpenAI Chat Completions com tool-calling.

A Lara tem duas tools:
- notify_qualified_lead: quando concluiu a qualificação
- request_human:         quando cliente pede atendente
"""

import json
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from openai import AsyncOpenAI
from loguru import logger

from app.core.config import get_settings
from app.models import Lead, Message
from app.services import notification_service

settings = get_settings()
_client = AsyncOpenAI(api_key=settings.openai_api_key)

_PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "lara_system_prompt.txt"


def _load_prompt() -> str:
    if not _PROMPT_PATH.exists():
        logger.warning(f"Prompt não encontrado em {_PROMPT_PATH}; usando fallback mínimo")
        return "Você é Lara, especialista em energia solar da FP Solar. Qualifique o lead com cordialidade."
    return _PROMPT_PATH.read_text(encoding="utf-8")


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "notify_qualified_lead",
            "description": (
                "Notifica o time interno da FP Solar via WhatsApp quando a qualificação do lead "
                "estiver completa (todos os dados coletados). Use APENAS uma vez no fim da qualificação."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "nome": {"type": "string", "description": "Nome completo do cliente"},
                    "email": {"type": "string", "description": "E-mail do cliente (opcional)"},
                    "cpf": {"type": "string", "description": "CPF do cliente (somente números, opcional)"},
                    "data_de_nascimento": {
                        "type": "string",
                        "description": "Data de nascimento em YYYY-MM-DD ou DD/MM/YYYY (opcional)",
                    },
                    "resumo_da_solicitacao": {
                        "type": "string",
                        "description": (
                            "Resumo completo da qualificação: tipo de projeto (residencial/rural/empresarial), "
                            "tipo de telhado, padrão de energia, cidade, valor médio da conta, e qualquer "
                            "observação relevante para o vendedor preparar a proposta."
                        ),
                    },
                },
                "required": ["nome", "resumo_da_solicitacao"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_human",
            "description": (
                "Aciona o time interno quando o cliente pede explicitamente para falar com um humano, "
                "ou quando você (Lara) não consegue ajudar e precisa transferir. Use com critério."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "resumo_do_pedido": {
                        "type": "string",
                        "description": "Resumo curto do que o cliente está pedindo / motivo da transferência",
                    },
                },
                "required": ["resumo_do_pedido"],
            },
        },
    },
]


_NAMES_GENERIC = {
    "cliente", "usuario", "usuário", "contato", "suporte", "atendimento",
    "vendedor", "corretor", "gerente", "diretor", "engenheiro", "tecnico",
    "técnico", "lead", "user", "guest", "anonimo", "anônimo", "sem nome",
    "whatsapp", "comercial",
}


def _nome_valido(name: str | None) -> str | None:
    """Retorna o primeiro nome se push_name parecer um nome humano real;
    None caso pareça empresa/genérico/telefone/etc."""
    if not name:
        return None
    n = name.strip()
    if not n:
        return None
    # Rejeita se tem dígito, @, + (telefones/emails)
    if any(c.isdigit() or c in "@+" for c in n):
        return None
    # Rejeita se for palavra genérica
    if n.lower() in _NAMES_GENERIC:
        return None
    # Rejeita se for muito curto
    first = n.split()[0]
    if len(first) < 2:
        return None
    return first.capitalize()


def _saudacao_por_horario(hora: int) -> str:
    if 5 <= hora < 12:
        return "Bom dia"
    if 12 <= hora < 18:
        return "Boa tarde"
    return "Boa noite"


def _build_context_header(lead: Lead) -> str:
    now = datetime.now(ZoneInfo(settings.timezone))
    saudacao = _saudacao_por_horario(now.hour)

    # Decide se já temos um nome utilizável
    nome_uso = _nome_valido(lead.full_name) or _nome_valido(lead.push_name)

    if nome_uso:
        diretiva_nome = (
            f"- **NOME DO CLIENTE: \"{nome_uso}\"** — JÁ SABEMOS o nome. "
            f"USE \"{nome_uso}\" em TODAS as saudações e respostas. "
            f"**NÃO pergunte o nome de novo.**"
        )
    else:
        diretiva_nome = (
            f"- **Nome do cliente AINDA NÃO INFORMADO** (push_name='{lead.push_name or ''}'). "
            f"**Pergunte o nome no primeiro turno**, sem usar nenhum nome até o cliente responder."
        )

    return (
        f"# CONTEXTO DA CONVERSA\n"
        f"- Data/Hora: {now.strftime('%A, %d/%m/%Y %H:%M')}\n"
        f"- **Saudação correta agora: \"{saudacao}\"** — use EXATAMENTE essa saudação ao abrir a conversa.\n"
        f"- WhatsApp: {lead.telefone}\n"
        f"{diretiva_nome}\n"
    )


def _history_to_openai(history: list[Message]) -> list[dict]:
    msgs: list[dict] = []
    for m in history:
        if m.role == "user":
            msgs.append({"role": "user", "content": m.content})
        elif m.role == "assistant":
            entry: dict = {"role": "assistant", "content": m.content or ""}
            msgs.append(entry)
        elif m.role == "tool":
            # Tool results não voltam exatamente como na chamada original do OpenAI,
            # então tratamos como nota interna do assistente para o LLM ter contexto.
            msgs.append({"role": "assistant", "content": f"[tool {m.tool_name} executada: {m.content}]"})
    return msgs


async def chat(lead: Lead, user_text: str, history: list[Message]) -> tuple[str, list[dict]]:
    """Retorna (resposta_texto, lista_de_tools_executadas).

    tools_executadas = [{"name": str, "args": dict, "result": dict}]
    """
    system = _load_prompt() + "\n\n" + _build_context_header(lead)
    messages: list[dict] = [{"role": "system", "content": system}]
    messages += _history_to_openai(history)
    messages.append({"role": "user", "content": user_text})

    tools_executed: list[dict] = []
    max_iters = 4  # evita loop infinito

    for _ in range(max_iters):
        resp = await _client.chat.completions.create(
            model=settings.openai_model,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
            temperature=0.4,
        )
        choice = resp.choices[0]
        msg = choice.message

        if not msg.tool_calls:
            return (msg.content or "").strip(), tools_executed

        # Acrescenta a chamada de tool ao histórico do diálogo
        messages.append(
            {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in msg.tool_calls
                ],
            }
        )

        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}

            result = await _dispatch_tool(tc.function.name, args, lead.telefone)
            tools_executed.append({"name": tc.function.name, "args": args, "result": result})

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "name": tc.function.name,
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )

    logger.warning(f"chat hit max_iters para lead {lead.telefone}")
    return "Tudo certo! Já encaminhei suas informações para a nossa equipe. 🙌", tools_executed


async def _dispatch_tool(name: str, args: dict, phone: str) -> dict:
    logger.info(f"[tool] {name} args={args}")
    if name == "notify_qualified_lead":
        return await notification_service.notify_qualified_lead(phone, **args)
    if name == "request_human":
        return await notification_service.request_human(phone, **args)
    return {"ok": False, "error": f"tool_not_found:{name}"}
