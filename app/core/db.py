from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine, AsyncSession
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool
from app.core.config import get_settings

settings = get_settings()

# Supabase pooler em transaction mode (porta 6543) faz seu próprio pooling
# e NÃO suporta prepared statements compartilhados entre transações.
# Combo correto:
#   - NullPool (sem pooling no SQLAlchemy — deixa o pgbouncer cuidar)
#   - statement_cache_size=0 no asyncpg
#   - prepared_statement_cache_size=0 no dialect SQLAlchemy
#   - prepared_statement_name_func: nomes únicos por sessão (defensivo)
import uuid

def _ps_name(*_):
    return f"__sa_pst_{uuid.uuid4().hex[:8]}__"

engine = create_async_engine(
    settings.database_url,
    poolclass=NullPool,
    connect_args={
        "statement_cache_size": 0,
        "prepared_statement_cache_size": 0,
        "prepared_statement_name_func": _ps_name,
    },
)

AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


class Base(DeclarativeBase):
    pass


async def get_session() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session
