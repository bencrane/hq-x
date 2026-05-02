from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from psycopg_pool import AsyncConnectionPool

from app.config import settings

_pool: AsyncConnectionPool | None = None


async def init_pool() -> None:
    global _pool
    if _pool is not None:
        return
    # Supabase pooled connections (port 6543) run in transaction-pooling mode,
    # which is incompatible with psycopg3's prepared-statement cache.
    _pool = AsyncConnectionPool(
        conninfo=str(settings.HQX_DB_URL_POOLED),
        min_size=2,
        max_size=10,
        open=False,
        kwargs={"prepare_threshold": None},
    )
    await _pool.open()
    await _pool.wait()


async def close_pool() -> None:
    global _pool
    if _pool is None:
        return
    await _pool.close()
    _pool = None


@asynccontextmanager
async def get_db_connection() -> AsyncIterator:
    if _pool is None:
        raise RuntimeError("DB pool is not initialized")
    async with _pool.connection() as conn:
        yield conn
