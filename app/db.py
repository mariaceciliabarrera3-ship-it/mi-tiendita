import asyncio
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import aiosqlite
import asyncpg

from app.config import get_settings

_PARAM_RE = re.compile(r"\$(\d+)")
_FOR_UPDATE_RE = re.compile(r"\s+FOR\s+UPDATE\b", re.IGNORECASE)


def translate_pg_to_sqlite(sql: str, args: tuple[Any, ...]) -> tuple[str, list[Any]]:
    """
    Traduce SQL parametrizado estilo asyncpg ($1, $2...) a marcadores posicionales SQLite (?)
    respetando literales entre comillas simples, y elimina cláusulas FOR UPDATE (reemplazadas
    en SQLite por bloqueo transaccional BEGIN IMMEDIATE).
    """
    sql_clean = _FOR_UPDATE_RE.sub("", sql)
    out_chars: list[str] = []
    sqlite_args: list[Any] = []
    in_single_quote = False
    i = 0
    n = len(sql_clean)

    while i < n:
        ch = sql_clean[i]
        if ch == "'":
            out_chars.append(ch)
            if in_single_quote and i + 1 < n and sql_clean[i + 1] == "'":
                out_chars.append("'")
                i += 2
                continue
            in_single_quote = not in_single_quote
            i += 1
            continue

        if not in_single_quote and ch == "$":
            m = _PARAM_RE.match(sql_clean, i)
            if m:
                idx = int(m.group(1)) - 1
                if idx < 0 or idx >= len(args):
                    raise ValueError(f"Índice de parámetro ${idx + 1} fuera de rango.")
                val = _adapt_sqlite_param(args[idx])
                sqlite_args.append(val)
                out_chars.append("?")
                i = m.end()
                continue

        out_chars.append(ch)
        i += 1

    return "".join(out_chars), sqlite_args


def _adapt_sqlite_param(val: Any) -> Any:
    if isinstance(val, bool):
        return 1 if val else 0
    if isinstance(val, Decimal):
        return float(val)
    if isinstance(val, datetime):
        if val.tzinfo is None:
            val = val.replace(tzinfo=timezone.utc)
        return val.isoformat()
    return val


def _adapt_pg_param(val: Any) -> Any:
    if isinstance(val, str):
        return val
    if isinstance(val, datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=timezone.utc)
        return val
    return val


def _normalize_row(record: Any) -> dict[str, Any]:
    d = dict(record)
    for k, v in d.items():
        if isinstance(v, datetime):
            if v.tzinfo is None:
                v = v.replace(tzinfo=timezone.utc)
            d[k] = v.isoformat()
    return d


def parse_postgres_dsn(raw_url: str) -> tuple[str, Any]:
    """
    Normaliza URLs de Neon / Render (postgresql:// o postgres:// con ?sslmode=require)
    para compatibilidad nativa con asyncpg (que recibe ssl por parámetro separado).
    """
    url = raw_url.strip()
    if url.startswith("postgresql+asyncpg://"):
        url = url.replace("postgresql+asyncpg://", "postgresql://", 1)
    elif url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)

    parts = urlsplit(url)
    query_items = parse_qsl(parts.query, keep_blank_values=True)
    clean_query: list[tuple[str, str]] = []
    ssl_mode: Any = None

    for k, v in query_items:
        kl = k.lower()
        if kl == "sslmode":
            if v in ("require", "verify-ca", "verify-full", "prefer"):
                ssl_mode = "require" if v == "require" else v
            elif v == "disable":
                ssl_mode = False
        elif kl == "channel_binding":
            # asyncpg negocia SCRAM-SHA-256 automáticamente; ignora channel_binding en DSN
            continue
        else:
            clean_query.append((k, v))

    clean_dsn = urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(clean_query), parts.fragment)
    )
    return clean_dsn, ssl_mode


class DBConnectionWrapper:
    """Interfaz unificada de consultas SQL nativas sin ORM sobre asyncpg o aiosqlite."""

    def __init__(self, backend: str, raw_conn: Any) -> None:
        self.backend = backend
        self._conn = raw_conn

    async def execute(self, sql: str, *args: Any) -> str:
        if self.backend == "postgres":
            pg_args = tuple(_adapt_pg_param(a) for a in args)
            return await self._conn.execute(sql, *pg_args)
        q, sq_args = translate_pg_to_sqlite(sql, args)
        cur = await self._conn.execute(q, sq_args)
        await cur.close()
        return "OK"

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        if self.backend == "postgres":
            pg_args = tuple(_adapt_pg_param(a) for a in args)
            rows = await self._conn.fetch(sql, *pg_args)
            return [_normalize_row(r) for r in rows]
        q, sq_args = translate_pg_to_sqlite(sql, args)
        cur = await self._conn.execute(q, sq_args)
        rows = await cur.fetchall()
        await cur.close()
        return [_normalize_row(r) for r in rows]

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        if self.backend == "postgres":
            pg_args = tuple(_adapt_pg_param(a) for a in args)
            row = await self._conn.fetchrow(sql, *pg_args)
            return _normalize_row(row) if row is not None else None
        q, sq_args = translate_pg_to_sqlite(sql, args)
        cur = await self._conn.execute(q, sq_args)
        row = await cur.fetchone()
        await cur.close()
        return _normalize_row(row) if row is not None else None

    async def fetchval(self, sql: str, *args: Any) -> Any:
        if self.backend == "postgres":
            pg_args = tuple(_adapt_pg_param(a) for a in args)
            return await self._conn.fetchval(sql, *pg_args)
        row = await self.fetchrow(sql, *args)
        if row is None:
            return None
        return next(iter(row.values()))


class DatabaseManager:
    """
    Gestor asíncrono de base de datos con soporte dual:
    - PostgreSQL nativo mediante asyncpg (Neon Serverless / Producción) + Row-Level Security
    - SQLite WAL mediante aiosqlite (Desarrollo cero-config / Pruebas / Modo portable)
    """

    def __init__(self, database_url: str | None = None) -> None:
        settings = get_settings()
        self.database_url = database_url or settings.database_url
        self.pool_min = settings.db_pool_min_size
        self.pool_max = settings.db_pool_max_size
        self.backend = (
            "postgres"
            if self.database_url.startswith(("postgresql://", "postgres://", "postgresql+asyncpg://"))
            else "sqlite"
        )
        self._pg_pool: asyncpg.Pool | None = None
        self._sqlite_path: str = ""
        self._sqlite_write_lock = asyncio.Lock()
        self._initialized = False

    def _resolve_sqlite_path(self) -> str:
        raw = self.database_url
        if raw.startswith("sqlite:///"):
            path_str = raw[len("sqlite:///") :]
        elif raw.startswith("sqlite://"):
            path_str = raw[len("sqlite://") :]
        else:
            path_str = raw
        if path_str == ":memory:":
            return ":memory:"
        p = Path(path_str)
        p.parent.mkdir(parents=True, exist_ok=True)
        return str(p)

    async def connect(self) -> None:
        if self._initialized:
            return
        if self.backend == "postgres":
            dsn, ssl_mode = parse_postgres_dsn(self.database_url)
            kwargs: dict[str, Any] = {
                "min_size": self.pool_min,
                "max_size": self.pool_max,
                "command_timeout": 30,
                # Compatible con PgBouncer transaction-mode de Neon Pooler:
                "statement_cache_size": 0,
            }
            if ssl_mode is not None:
                kwargs["ssl"] = ssl_mode
            self._pg_pool = await asyncpg.create_pool(dsn, **kwargs)
        else:
            self._sqlite_path = self._resolve_sqlite_path()
            async with aiosqlite.connect(self._sqlite_path) as conn:
                await conn.execute("PRAGMA journal_mode=WAL;")
                await conn.execute("PRAGMA synchronous=NORMAL;")
                await conn.execute("PRAGMA foreign_keys=ON;")
                await conn.commit()
        self._initialized = True

    async def disconnect(self) -> None:
        if self._pg_pool is not None:
            await self._pg_pool.close()
            self._pg_pool = None
        self._initialized = False

    @asynccontextmanager
    async def connection(self, tenant_id: str | None = None) -> AsyncIterator[DBConnectionWrapper]:
        """ Obtiene conexión de lectura (con contexto RLS si corresponde). """
        if not self._initialized:
            await self.connect()

        if self.backend == "postgres":
            assert self._pg_pool is not None
            async with self._pg_pool.acquire() as raw_conn:
                if tenant_id:
                    async with raw_conn.transaction():
                        await raw_conn.execute(
                            "SELECT set_config('app.current_tenant_id', $1, true)",
                            str(tenant_id),
                        )
                        yield DBConnectionWrapper("postgres", raw_conn)
                else:
                    yield DBConnectionWrapper("postgres", raw_conn)
        else:
            async with aiosqlite.connect(self._sqlite_path, timeout=15.0) as raw_conn:
                raw_conn.row_factory = aiosqlite.Row
                await raw_conn.execute("PRAGMA foreign_keys=ON;")
                await raw_conn.execute("PRAGMA busy_timeout=10000;")
                yield DBConnectionWrapper("sqlite", raw_conn)

    @asynccontextmanager
    async def transaction(self, tenant_id: str | None = None) -> AsyncIterator[DBConnectionWrapper]:
        """
        Ejecuta una transacción ACID atómica:
        - En PostgreSQL: transacción nativa + set_config('app.current_tenant_id', tenant_id, true) para RLS
          y soporte de bloqueo pesimista SELECT ... FOR UPDATE.
        - En SQLite: serialización por asyncio.Lock + BEGIN IMMEDIATE garantizando aislamiento serializable
          y evitando condiciones de carrera en concurrencia alta.
        """
        if not self._initialized:
            await self.connect()

        if self.backend == "postgres":
            assert self._pg_pool is not None
            async with self._pg_pool.acquire() as raw_conn:
                async with raw_conn.transaction():
                    if tenant_id:
                        await raw_conn.execute(
                            "SELECT set_config('app.current_tenant_id', $1, true)",
                            str(tenant_id),
                        )
                    # Serializar mutaciones del comercio, incluidos números correlativos,
                    # idempotencia, arqueos y límites entre procesos/instancias.
                    if tenant_id:
                        await raw_conn.fetchrow("SELECT id FROM tenants WHERE id = $1 FOR UPDATE", str(tenant_id))
                    yield DBConnectionWrapper("postgres", raw_conn)
        else:
            async with self._sqlite_write_lock:
                async with aiosqlite.connect(
                    self._sqlite_path, timeout=15.0, isolation_level=None
                ) as raw_conn:
                    raw_conn.row_factory = aiosqlite.Row
                    await raw_conn.execute("PRAGMA foreign_keys=ON;")
                    await raw_conn.execute("PRAGMA busy_timeout=10000;")
                    await raw_conn.execute("BEGIN IMMEDIATE;")
                    try:
                        yield DBConnectionWrapper("sqlite", raw_conn)
                        await raw_conn.execute("COMMIT;")
                    except Exception:
                        await raw_conn.execute("ROLLBACK;")
                        raise


# Singleton global por defecto de la aplicación (reemplazable en pruebas)
db = DatabaseManager()


def get_db() -> DatabaseManager:
    return db


def set_db(custom_db: DatabaseManager) -> None:
    global db
    db = custom_db
