"""Smoke test do openai_service.chat() sem passar pelo uazapi.

Cria um lead fake direto no banco, simula 'Oi' + 'queria orçamento solar'
e imprime o que a Lara responderia.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import contact_updater, openai_service


async def main():
    phone = "5573999000111"
    lead = await contact_updater.get_or_create_lead(phone, push_name="Teste")
    print(f"lead.id = {lead.id}  ia_on_off = {lead.ia_on_off}")

    history = await contact_updater.load_history(lead.id)
    print(f"história prévia: {len(history)} mensagens")

    reply, tools = await openai_service.chat(lead, "Oi, tudo bem?", history)
    print("\n── Lara respondeu ──")
    print(reply)
    print(f"\ntools executadas: {len(tools)}")
    for t in tools:
        print(" -", t["name"], "→", t["result"])


if __name__ == "__main__":
    asyncio.run(main())
