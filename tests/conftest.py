import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

# Примусово (не setdefault): тести ніколи не повинні торкатися реальної БД
os.environ["DB_LINK_CONNECTION"] = "postgresql+asyncpg://dummy:dummy@127.0.0.1:1/dummy"

from bot.data import db_connection
from bot.data.models import Base

ROOT = Path(__file__).resolve().parent.parent
TEST_URL = os.getenv("TEST_DATABASE_URL", "postgresql+asyncpg://postgres:test@127.0.0.1:55432/tgb_test")
HINT = ("Postgres для тестів недоступний. Запусти: docker run -d --name tgb-test-pg -p 127.0.0.1:55432:5432 "
        "-e POSTGRES_PASSWORD=test -e POSTGRES_DB=tgb_test postgres:16-alpine")


async def _reset_schema() -> None:
    engine = create_async_engine(TEST_URL, poolclass=NullPool, connect_args={"timeout": 3})
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
    finally:
        await engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def pg_schema():
    """Схема тестової БД = справжня міграція (alembic upgrade head), а не create_all."""
    name = make_url(TEST_URL).database or ""
    if "test" not in name:  # захист від DROP SCHEMA у чужій базі
        pytest.exit(f"TEST_DATABASE_URL: ім'я БД '{name}' має містити 'test'", returncode=2)
    try:
        asyncio.run(_reset_schema())
    except Exception as e:
        pytest.exit(f"{HINT} ({type(e).__name__})", returncode=2)
    env = {**os.environ, "DB_LINK_CONNECTION": TEST_URL}
    res = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, env=env,
                         capture_output=True, text=True)
    if res.returncode != 0:
        pytest.exit(f"alembic upgrade head failed:\n{res.stderr[-2000:]}", returncode=2)
    yield


@pytest_asyncio.fixture
async def db(monkeypatch, pg_schema):
    engine = create_async_engine(TEST_URL, poolclass=NullPool, connect_args={"timeout": 3, "command_timeout": 30})
    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    monkeypatch.setattr(db_connection, "engine", engine)
    monkeypatch.setattr(db_connection, "async_session", session_factory)
    # репозиторії імпортують async_session напряму — підміняємо в кожному модулі
    for name, mod in list(sys.modules.items()):
        if name.startswith("bot.data.repositories.") and hasattr(mod, "async_session"):
            monkeypatch.setattr(mod, "async_session", session_factory)
    yield engine
    await engine.dispose()
