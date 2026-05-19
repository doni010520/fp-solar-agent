import uuid
from datetime import date, datetime
from decimal import Decimal
from sqlalchemy import String, Date, DateTime, Numeric, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from app.core.db import Base


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    telefone: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    full_name: Mapped[str | None] = mapped_column(String)
    push_name: Mapped[str | None] = mapped_column(String)
    email: Mapped[str | None] = mapped_column(String)
    cpf: Mapped[str | None] = mapped_column(String)
    data_nascimento: Mapped[date | None] = mapped_column(Date)

    tipo_projeto: Mapped[str | None] = mapped_column(String)
    tipo_telhado: Mapped[str | None] = mapped_column(String)
    padrao_energia: Mapped[str | None] = mapped_column(String)
    cidade: Mapped[str | None] = mapped_column(String)
    valor_conta_luz: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    observacoes: Mapped[str | None] = mapped_column(Text)

    status_funil_vendas: Mapped[str] = mapped_column(String, default="novo")
    etapa_follow_up: Mapped[str] = mapped_column(String, default="aguardando_primeira_mensagem")
    ia_on_off: Mapped[str] = mapped_column(String, default="ON")

    ultimo_contato: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
