"""Simula uma conversa completa de qualificação até a tool ser chamada."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import contact_updater, openai_service


async def turn(lead, text):
    history = await contact_updater.load_history(lead.id)
    await contact_updater.save_message(lead.id, "user", text)
    reply, tools = await openai_service.chat(lead, text, history)
    await contact_updater.save_message(lead.id, "assistant", reply)
    for t in tools:
        await contact_updater.save_message(
            lead.id, "tool", str(t["result"]),
            tool_name=t["name"], tool_args=t.get("args"),
        )
    print(f"\n👤 Cliente: {text}")
    print(f"🤖 Lara: {reply}")
    if tools:
        print(f"   ⚡ tools chamadas: {[t['name'] for t in tools]}")
        for t in tools:
            print(f"      args: {t['args']}")
            print(f"      result: {t['result']}")
    return tools


async def main():
    phone = "5573999000222"
    lead = await contact_updater.get_or_create_lead(phone, push_name="Mock Cliente")
    print(f"lead criado: {lead.id}")

    msgs = [
        "Oi, tudo bem?",
        "Meu nome é João Silva, queria orçamento de energia solar",
        "Sou de Itabuna-BA, tenho uma casa residencial, telhado colonial de barro",
        "Padrão bifásico, conta vem uns 450 reais por mês",
        "Meu cpf é 12345678900, nasci em 15/03/1985, joao@email.com",
    ]
    for m in msgs:
        tools = await turn(lead, m)
        if any(t["name"] == "notify_qualified_lead" for t in tools):
            print("\n✅ TOOL DISPARADA — qualificação completa!")
            break


if __name__ == "__main__":
    asyncio.run(main())
