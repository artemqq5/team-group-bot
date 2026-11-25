---
name: sqlalchemy-async
description: Conventions for SQLAlchemy 2.0 async ORM in team-group-bot — MySQL via aiomysql (DB_LINK_CONNECTION), engine + async_session in bot/data/db_connection.py, DeclarativeBase models in bot/data/models.py (Telegram ids stored as String(255)), static-method repositories in bot/data/repositories/ that open their own session per call, log_exc_with_try_except decorator, select()/scalars() 2.0-style queries, MySQL upserts, expunge before returning. Use when working on models, repositories, queries, AsyncSession, MissingGreenlet errors, MySQL connection errors, or DB tests.
---

# SQLAlchemy 2.0 async

> **Since the Postgres move the DB is PostgreSQL 16 via asyncpg** (`postgresql+asyncpg://…@postgres:5432/db`). MySQL-specific notes below are legacy; for current dialect rules (pg upsert, NUL/length, tests on real Postgres) see skill `impl-mysql-to-pg`.

Only 2.0 style: `select()`, `Mapped[]`, `mapped_column()`. No `session.query()`.

## Engine & session — `bot/data/db_connection.py`

```python
engine = create_async_engine(os.getenv("DB_LINK_CONNECTION"), pool_pre_ping=True, pool_recycle=28000)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
```

- URL form: `mysql+aiomysql://user:pass@host:3306/db`; MySQL runs on the host (`host.docker.internal` from Docker).
- `pool_recycle` < MySQL `wait_timeout` — avoids "MySQL server has gone away"; keep `pool_pre_ping`.
- PyMySQL is pinned to 1.1.1 (newer broke DB access) — don't bump it casually.

## Models — `bot/data/models.py`

- `class Base(DeclarativeBase)`; tables: `users`, `chats`, `admins`, `broadcasts`.
- Telegram ids (`user_id`, `group_id`, `admin_id`) are **`String(255)`** — always `str(id)` when writing and comparing. Keep the convention; don't mix in BigInteger columns.
- Python attr ≠ column name exists (`title_group` → column `"title_gruop"`); keep explicit column names when renaming attrs.
- Booleans: `Boolean` with `server_default="0"` (MySQL tinyint).

## Repositories — `bot/data/repositories/*Repository.py`

Pattern: class with `@staticmethod` async methods, each opens its own session:

```python
@staticmethod
@log_exc_with_try_except()          # logs and returns None on any exception
async def add_chat(group_id, title, datetime) -> Optional[ChatModel]:
    async with async_session() as session:
        async with session.begin():
            chat = ChatModel(group_id=str(group_id), title=title, time=datetime)
            session.add(chat)
            await session.flush()
            await session.refresh(chat)
            session.expunge(chat)
            return chat
```

- Writes inside `async with session.begin()` (auto-commit/rollback).
- Return detached objects (`expunge` or plain values); never touch lazy relationships after the session closes.
- Because of the decorator, `None` means "error OR not found" — callers must handle `None`; for write methods where the distinction matters, return `bool` with explicit try/except (see `remove_chat`).
- Handlers call repositories only; no SQL in routers.
- Bulk changes: one `update(...).where(Model.group_id.in_(ids))` per batch, not a commit per row.

## MySQL specifics

- Upsert: `from sqlalchemy.dialects.mysql import insert` → `insert(M).values(...).on_duplicate_key_update(col=...)`.
- No `RETURNING` on MySQL — use `flush()` + `refresh()` to get values.
- `String` needs a length; `Text` can't be a plain index/PK.
- Case-insensitive collation by default: string comparisons ignore case.

## Testing

No test suite yet. If tests are added: use a separate MySQL test DB (never the live one) or mock the repository layer; schema via `alembic upgrade head`.
