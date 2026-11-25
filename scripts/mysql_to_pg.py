"""Одноразове копіювання даних MySQL -> PostgreSQL з перевіркою.

    python -m scripts.mysql_to_pg [--truncate] [--dry-run]

Джерело: MYSQL_SOURCE_URL (mysql+aiomysql://...?charset=utf8mb4), лише читання.
Ціль:    DB_LINK_CONNECTION (postgresql+asyncpg://...), схема вже створена `alembic upgrade head`.
--dry-run: копіює в тимчасову БД на тому ж PG-сервері, перевіряє і видаляє її (бойова ціль не чіпається).
Тексти повідомлень ніколи не друкуються: лише лічильники та ключі рядків.
"""
import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Boolean, DateTime, JSON, String, Text, func, insert, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from bot.data.models import Base
from bot.data.other.accesses import TypeOfAdmins

ROOT = Path(__file__).resolve().parent.parent
CHUNK = 1000

# (таблиця, обов'язкова в джерелі)
TABLES = [("admins", True), ("chats", True), ("users", True), ("broadcasts", True),
          ("chat_messages", False), ("mcp_keys", False), ("mcp_audit_log", False)]
# ідентифікатори: NUL -> abort, ніколи не змінюємо (інакше health-check видалить групи, яких Telegram "не знає")
ID_COLS = {
    "admins": {"telegram_id"}, "chats": {"group_id"}, "users": {"user_id", "group_id"},
    "broadcasts": {"admin_id"}, "chat_messages": {"group_id", "author_id"},
    "mcp_keys": {"created_by", "revoked_by"}, "mcp_audit_log": set(),
}
SEQ_TABLES = ["broadcasts", "chat_messages", "mcp_keys", "mcp_audit_log"]
ID_RE = re.compile(r"-?\d+")


class CopyAbort(Exception):
    pass


def _tables() -> dict:
    return {name: Base.metadata.tables[name] for name, _ in TABLES}


def _is_text(col) -> bool:
    return isinstance(col.type, (String, Text))


def _is_dt(col) -> bool:
    return isinstance(col.type, DateTime)


def _text_cols(table) -> list:
    return [c.name for c in table.columns if _is_text(c)]


def _dt_cols(table) -> list:
    return [c.name for c in table.columns if _is_dt(c)]


def check_columns(table, source_cols) -> None:
    """Склад колонок джерела має збігатися з моделлю: невідома колонка = можлива втрата даних."""
    expected = {c.name for c in table.columns}
    got = set(source_cols)
    if got - expected:
        raise CopyAbort(f"{table.name}: unknown source columns {sorted(got - expected)}")
    if expected - got:
        raise CopyAbort(f"{table.name}: source misses columns {sorted(expected - got)}")


def transform_row(table, raw: dict, stats: Counter) -> dict:
    """Чиста функція: рядок MySQL -> рядок для PG. Лічильники змін — у stats. Тексти не логуються."""
    out = {}
    pk = tuple(raw[c.name] for c in table.primary_key.columns)
    ids = ID_COLS[table.name]
    for col in table.columns:
        v = raw[col.name]
        where = f"{table.name}.{col.name} pk={pk!r}"
        if isinstance(v, bytes):
            v = v.decode("utf-8")
        if v is not None and _is_text(col):
            if col.name in ids:
                if "\x00" in v:
                    raise CopyAbort(f"NUL in identifier {where}")
                if not ID_RE.fullmatch(v):
                    stats[f"non_numeric_id:{table.name}.{col.name}"] += 1  # лише звіт, значення не змінюємо
            elif "\x00" in v:
                stats[f"nul_removed:{table.name}.{col.name}"] += v.count("\x00")
                v = v.replace("\x00", "")
            if table.name == "admins" and col.name == "role" and v != v.upper() \
                    and v.upper() in {t.value for t in TypeOfAdmins}:
                stats["admin_role_uppercased"] += 1
                v = v.upper()
        elif v is not None and isinstance(col.type, Boolean):
            v = bool(v)
        elif isinstance(col.type, DateTime) and v is not None:
            if isinstance(v, str):
                try:
                    v = datetime.fromisoformat(v)
                except ValueError:
                    v = None  # MySQL zero-date ('0000-00-00 ...') -> None
                    stats[f"zero_date:{table.name}.{col.name}"] += 1
        elif isinstance(col.type, JSON) and isinstance(v, (str, bytes)):
            v = json.loads(v)
        if v is None and not col.nullable:
            raise CopyAbort(f"NULL/zero-date in NOT NULL column {where}")
        out[col.name] = v
    return out


def _norm(v):
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, (dict, list)):
        return json.dumps(v, sort_keys=True, ensure_ascii=False)
    return v


def checksum(table, rows: list[dict]) -> str:
    """sha256 по рядках, відсортованих у Python за PK (колації MySQL/PG сортують рядки по-різному)."""
    pk = [c.name for c in table.primary_key.columns]
    h = hashlib.sha256()
    for r in sorted(rows, key=lambda r: tuple(r[c] for c in pk)):
        h.update(json.dumps([_norm(r[c.name]) for c in table.columns], ensure_ascii=False).encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def py_aggregates(table, rows: list[dict], nul_removed: dict | None = None) -> dict:
    """Агрегати з уже прочитаних рядків: count, SUM(len) текстових колонок, MIN/MAX дат."""
    agg = {"count": len(rows)}
    for c in _text_cols(table):
        agg[f"len:{c}"] = sum(len(r[c]) for r in rows if r[c] is not None) + (nul_removed or {}).get(c, 0)
    for c in _dt_cols(table):
        vals = [r[c] for r in rows if isinstance(r[c], datetime) and r[c] >= datetime(1000, 1, 1)]
        agg[f"min:{c}"] = min(vals) if vals else None
        agg[f"max:{c}"] = max(vals) if vals else None
    return agg


def _agg_sql(table, length_fn: str) -> str:
    parts = ["COUNT(*)"]
    for c in _text_cols(table):
        parts.append(f"SUM({length_fn}({_q(c)}))")
    for c in _dt_cols(table):
        parts.append(f"MIN(CASE WHEN {_q(c)} >= '1000-01-01' THEN {_q(c)} END)")
        parts.append(f"MAX(CASE WHEN {_q(c)} >= '1000-01-01' THEN {_q(c)} END)")
    return f"SELECT {', '.join(parts)} FROM {_q(table.name)}"


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _agg_from_row(table, row) -> dict:
    agg, i = {"count": int(row[0])}, 1
    for c in _text_cols(table):
        agg[f"len:{c}"] = int(row[i] or 0)
        i += 1
    for c in _dt_cols(table):
        agg[f"min:{c}"], agg[f"max:{c}"] = row[i], row[i + 1]
        i += 2
    return agg


@dataclass
class Source:
    rows: dict = field(default_factory=dict)          # table -> [transformed rows]
    stats: Counter = field(default_factory=Counter)
    server_agg: dict = field(default_factory=dict)    # table -> агрегати, пораховані самим MySQL
    raw_agg: dict = field(default_factory=dict)       # table -> агрегати з прочитаних сирих рядків


async def read_source(url: str) -> Source:
    u = make_url(url)
    if u.query.get("charset") != "utf8mb4":
        raise CopyAbort("MYSQL_SOURCE_URL must contain charset=utf8mb4")
    engine = create_async_engine(u, poolclass=NullPool)
    src = Source()
    try:
        async with engine.connect() as conn:
            # лише читання: і сесія, і транзакція (консистентний знімок)
            await conn.exec_driver_sql("SET SESSION TRANSACTION READ ONLY")
            await conn.exec_driver_sql("START TRANSACTION READ ONLY, WITH CONSISTENT SNAPSHOT")
            if (await conn.exec_driver_sql("SELECT @@session.transaction_read_only")).scalar_one() != 1:
                raise CopyAbort("MySQL session is not read-only")
            for name, required in TABLES:
                table = Base.metadata.tables[name]
                cols = (await conn.exec_driver_sql(
                    "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s", (name,))).scalars().all()
                cols = [c.decode() if isinstance(c, bytes) else c for c in cols]
                if not cols:
                    if required:
                        raise CopyAbort(f"source table {name} not found")
                    print(f"{name}: not in source, skipped")
                    continue
                check_columns(table, cols)
                names = ", ".join(f"`{c.name}`" for c in table.columns)
                res = await conn.exec_driver_sql(f"SELECT {names} FROM `{name}`")
                raw = [dict(zip([c.name for c in table.columns], r)) for r in res.fetchall()]
                src.raw_agg[name] = py_aggregates(table, [
                    {k: (v.decode() if isinstance(v, bytes) else v) for k, v in r.items()} for r in raw])
                src.rows[name] = [transform_row(table, r, src.stats) for r in raw]
                nul = {k.split(".", 1)[1]: n for k, n in src.stats.items()
                       if k.startswith(f"nul_removed:{name}.")}
                src.server_agg[name] = _agg_from_row(table, (await conn.exec_driver_sql(
                    _agg_sql(table, "CHAR_LENGTH").replace('"', "`"))).one())
                # агрегат MySQL має збігатися з прочитаним (ловить псування при читанні: charset, '?')
                if src.server_agg[name] != src.raw_agg[name]:
                    raise CopyAbort(f"{name}: MySQL aggregates differ from rows read "
                                    f"(charset/read corruption?)")
                src.raw_agg[name]["nul"] = nul
            await conn.rollback()
    finally:
        await engine.dispose()
    return src


async def _target_ready(conn, truncate: bool) -> None:
    ver = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalars().all()
    head = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini"))).get_current_head()
    if ver != [head]:
        raise CopyAbort(f"target alembic_version {ver} != head {head}; run alembic upgrade head")
    tables = _tables()
    non_empty = [n for n, t in tables.items()
                 if (await conn.execute(select(func.count()).select_from(t))).scalar_one() > 0]
    if non_empty and not truncate:
        raise CopyAbort(f"target tables not empty: {non_empty}; use --truncate (target only)")
    if non_empty:
        names = ", ".join(f'"{n}"' for n in tables)
        await conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))


async def write_and_verify(conn, src: Source) -> list[str]:
    """Пише в ціль і перевіряє ВСЕРЕДИНІ тієї ж транзакції; повертає список розбіжностей (порожній = ок)."""
    tables = _tables()
    for name, rows in src.rows.items():
        t = tables[name]
        for i in range(0, len(rows), CHUNK):
            await conn.execute(insert(t), rows[i:i + CHUNK])
    for name in SEQ_TABLES:
        if name in src.rows:
            await conn.execute(text(
                f"SELECT setval(pg_get_serial_sequence('{name}', 'id'), COALESCE(MAX(id), 1), MAX(id) IS NOT NULL) "
                f'FROM "{name}"'))
    problems = []
    for name, rows in src.rows.items():
        t = tables[name]
        pg_rows = [dict(r._mapping) for r in (await conn.execute(select(t))).all()]
        if len(pg_rows) != len(rows) or len(rows) != src.server_agg[name]["count"]:
            problems.append(f"{name}: row count mismatch")
        if checksum(t, pg_rows) != checksum(t, rows):
            problems.append(f"{name}: checksum mismatch")
        pg_agg = _agg_from_row(t, (await conn.execute(text(_agg_sql(t, "char_length")))).one())
        expected = dict(src.server_agg[name])
        for k, n in src.raw_agg[name].get("nul", {}).items():
            expected[f"len:{k}"] -= n  # NUL прибрано при копіюванні
        for k, v in expected.items():
            if pg_agg.get(k) != v:
                problems.append(f"{name}: aggregate {k} mismatch")
        # зразки: тільки ключі рядків
        if rows:
            pk = [c.name for c in t.primary_key.columns]
            srt = sorted(rows, key=lambda r: tuple(r[c] for c in pk))
            samples = [srt[0], srt[len(srt) // 2], srt[-1]]
            print(f"{name}: sample keys {[tuple(s[c] for c in pk) for s in samples]}")
    for name in SEQ_TABLES:
        if name in src.rows and src.rows[name]:
            # nextval не викликаємо: він би «з'їв» значення; pg_sequences.last_value = max(id) після setval
            last = (await conn.execute(text(
                f"SELECT last_value FROM pg_sequences WHERE sequencename = '{name}_id_seq'"))).scalar_one()
            if last != max(r["id"] for r in src.rows[name]):
                problems.append(f"{name}: sequence not reset")
    return problems


def _alembic_upgrade(url: str) -> None:
    res = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT,
                         env={**os.environ, "DB_LINK_CONNECTION": url}, capture_output=True, text=True)
    if res.returncode != 0:
        raise CopyAbort(f"alembic upgrade failed: {res.stderr[-500:]}")


async def _run(src_url: str, target_url: str, truncate: bool, dry_run: bool) -> int:
    t0 = time.monotonic()
    src = await read_source(src_url)
    print(f"read from MySQL: { {n: len(r) for n, r in src.rows.items()} } in {time.monotonic() - t0:.1f}s")
    tu = make_url(target_url)
    admin = None
    if dry_run:
        # тимчасова БД на тому ж сервері: перевіряє права, мережу, час і дані без зачіпання бойової
        tmp = f"{tu.database}_dryrun_{int(time.time())}"
        admin = create_async_engine(tu.set(database="postgres"), poolclass=NullPool, isolation_level="AUTOCOMMIT")
        async with admin.connect() as c:
            await c.execute(text(f'CREATE DATABASE "{tmp}"'))
        tu = tu.set(database=tmp)
    try:
        if dry_run:
            await asyncio.to_thread(_alembic_upgrade, tu.render_as_string(hide_password=False))
        engine = create_async_engine(tu, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                tx = await conn.begin()
                await _target_ready(conn, truncate)
                problems = await write_and_verify(conn, src)
                if problems:
                    await tx.rollback()
                    print("VERIFY FAILED (target rolled back):\n  " + "\n  ".join(problems))
                    return 1
                if dry_run:
                    await tx.rollback()
                else:
                    await tx.commit()
        finally:
            await engine.dispose()
    finally:
        if admin is not None:
            async with admin.connect() as c:
                await c.execute(text(f'DROP DATABASE IF EXISTS "{tu.database}" WITH (FORCE)'))
            await admin.dispose()
    print("transform report:", dict(src.stats) or "no changes")
    print(f"OK ({'dry-run, nothing committed' if dry_run else 'committed'}) in {time.monotonic() - t0:.1f}s")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--truncate", action="store_true", help="очистити таблиці цілі (лише PG) перед копіюванням")
    ap.add_argument("--dry-run", action="store_true", help="копія у тимчасову БД, потім видалення")
    a = ap.parse_args()
    src, tgt = os.getenv("MYSQL_SOURCE_URL"), os.getenv("DB_LINK_CONNECTION")
    if not src or not tgt:
        print("MYSQL_SOURCE_URL and DB_LINK_CONNECTION must be set")
        return 2
    if not tgt.startswith("postgresql"):
        print("DB_LINK_CONNECTION must be postgresql+asyncpg://")
        return 2
    try:
        return asyncio.run(_run(src, tgt, a.truncate, a.dry_run))
    except CopyAbort as e:
        print(f"ABORT: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
