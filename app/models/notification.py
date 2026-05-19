import uuid
from datetime import datetime
from sqlalchemy import String, DateTime, Text, Boolean, ForeignKey
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from app.core.db import Base


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    lead_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), nullable=False)
    tipo: Mapped[str] = mapped_column(String, nullable=False)  # qualified_lead | human_request
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    mensagem: Mapped[str] = mapped_column(Text, nullable=False)
    enviado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sucesso: Mapped[bool] = mapped_column(Boolean, default=True)
    erro: Mapped[str | None] = mapped_column(Text)
