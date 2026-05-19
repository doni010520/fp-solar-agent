"""Mostra a mensagem que seria enviada ao grupo interno SEM enviar de verdade."""
import asyncio, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import date
from app.services import contact_updater
from app.services.notification_service import _format_qualified_lead, _format_human_request


async def main():
    phone = "5573999000333"
    lead = await contact_updater.get_or_create_lead(phone, "Cliente Preview")
    await contact_updater.update_lead(
        phone,
        full_name="João da Silva",
        email="joao@email.com",
        cpf="123.456.789-00",
        data_nascimento=date(1985, 3, 15),
    )
    # recarrega
    from sqlalchemy import select
    from app.core.db import AsyncSessionLocal
    from app.models import Lead
    async with AsyncSessionLocal() as s:
        lead = (await s.execute(select(Lead).where(Lead.telefone == phone))).scalar_one()

    resumo = (
        "Cliente João da Silva, residencial, Itabuna-BA. "
        "Telhado colonial (barro), padrão bifásico. "
        "Conta de luz: R$ 450/mês. Sem equipamentos extras planejados. "
        "Pagamento: financiamento sem entrada."
    )

    print("="*60)
    print("⭐ NOVO LEAD QUALIFICADO — preview do que iria pro grupo:")
    print("="*60)
    print(_format_qualified_lead(lead, resumo))
    print()
    print("="*60)
    print("🚨 ATENDIMENTO HUMANO — preview:")
    print("="*60)
    print(_format_human_request(lead, "Cliente quer falar com vendedor humano sobre detalhes de financiamento."))


if __name__ == "__main__":
    asyncio.run(main())
