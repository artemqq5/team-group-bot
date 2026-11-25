"""Скрипт копіювання MySQL -> PG: перетворення (чисті) + запис/перевірка на реальному Postgres."""
from collections import Counter
from datetime import datetime

import pytest
from sqlalchemy import func, select, text

from bot.data.models import Base
from scripts import mysql_to_pg as cp

T = Base.metadata.tables


def row(table, **kw):
    base = {c.name: None for c in T[table].columns}
    base.update(kw)
    return base


def test_transform_bool_zero_date_role_nul():
    st = Counter()
    r = cp.transform_row(T["admins"], row("admins", telegram_id="1", name="a\x00b", role="admin"), st)
    assert r["role"] == "ADMIN" and r["name"] == "ab"
    assert st["admin_role_uppercased"] == 1 and st["nul_removed:admins.name"] == 1
    r = cp.transform_row(T["admins"], row("admins", telegram_id="2", role="custom"), st)
    assert r["role"] == "custom"  # невідому роль не чіпаємо
    c = cp.transform_row(T["chats"], row("chats", group_id="-1", time="0000-00-00 00:00:00",
                                         **{f: 1 for f in ("creo", "shop_google", "shop_fb", "console", "apps",
                                                           "affiliate_mp", "partner_mp", "media_mt", "agency_google",
                                                           "agency_fb", "media_mp", "partner_mt")}), st)
    assert c["time"] is None and c["creo"] is True and st["zero_date:chats.time"] == 1


def test_transform_aborts():
    st = Counter()
    with pytest.raises(cp.CopyAbort, match="NUL in identifier"):
        cp.transform_row(T["chats"], row("chats", group_id="-1\x00"), st)
    with pytest.raises(cp.CopyAbort, match="NOT NULL"):
        cp.transform_row(T["broadcasts"], row("broadcasts", id=1, admin_id="1", category="c",
                                              sent_at="0000-00-00 00:00:00", has_photo=0, has_buttons=0,
                                              groups_count=0), st)
    cp.transform_row(T["admins"], row("admins", telegram_id="abc"), st)
    assert st["non_numeric_id:admins.telegram_id"] == 1  # лише звіт


def test_transform_json_string():
    r = cp.transform_row(T["mcp_keys"], row("mcp_keys", id=1, token_hash="h", token_prefix="p", label="l",
                                            created_by="1", created_at=datetime(2025, 1, 1), categories='["creo"]',
                                            revoked=0), Counter())
    assert r["categories"] == ["creo"]


def test_check_columns():
    cols = [c.name for c in T["admins"].columns]
    cp.check_columns(T["admins"], cols)
    with pytest.raises(cp.CopyAbort, match="unknown"):
        cp.check_columns(T["admins"], cols + ["extra"])
    with pytest.raises(cp.CopyAbort, match="misses"):
        cp.check_columns(T["admins"], cols[:-1])


def test_checksum_order_independent_and_sensitive():
    t = T["admins"]
    a, b = row("admins", telegram_id="1", name="x"), row("admins", telegram_id="2", name="y")
    assert cp.checksum(t, [a, b]) == cp.checksum(t, [b, a])
    assert cp.checksum(t, [a, b]) != cp.checksum(t, [a, dict(b, name="z")])


async def test_charset_required():
    with pytest.raises(cp.CopyAbort, match="utf8mb4"):
        await cp.read_source("mysql+aiomysql://u:p@127.0.0.1:1/db")


def _source():
    flags = {f: False for f in ("creo", "shop_google", "shop_fb", "console", "apps", "affiliate_mp", "partner_mp",
                                "media_mt", "agency_google", "agency_fb", "media_mp", "partner_mt")}
    now = datetime(2025, 5, 5, 5, 5, 5)
    rows = {
        "admins": [row("admins", telegram_id="1", name="Адмін 😀", role="ADMIN")],
        "chats": [row("chats", group_id="-100", title="Гру́па", time=now, **dict(flags, creo=True))],
        "users": [row("users", user_id="7", group_id="-100", time=now), row("users", user_id="7", group_id="-101")],
        "broadcasts": [row("broadcasts", id=5, admin_id="1", category="creo", message_text="hi", has_photo=False,
                           has_buttons=False, sent_at=now, groups_count=1)],
    }
    src = cp.Source(rows=rows)
    for n, r in rows.items():
        src.server_agg[n] = cp.py_aggregates(T[n], r)
        src.raw_agg[n] = dict(src.server_agg[n])
    return src


async def _count(engine, table):
    async with engine.connect() as c:
        return (await c.execute(select(func.count()).select_from(T[table]))).scalar_one()


async def test_write_verify_commit_and_sequence(db):
    src = _source()
    async with db.connect() as conn:
        tx = await conn.begin()
        assert await cp.write_and_verify(conn, src) == []
        await tx.commit()
    assert await _count(db, "users") == 2
    async with db.begin() as conn:  # наступний broadcast = max+1
        nid = (await conn.execute(text(
            "INSERT INTO broadcasts (admin_id, category, sent_at) VALUES ('1','c',now()) RETURNING id"))).scalar_one()
    assert nid == 6


async def test_verify_detects_aggregate_mismatch(db):
    src = _source()
    src.server_agg["chats"]["len:title"] += 1  # ніби MySQL бачить інший вміст
    async with db.connect() as conn:
        tx = await conn.begin()
        problems = await cp.write_and_verify(conn, src)
        await tx.rollback()
    assert any("aggregate len:title" in p for p in problems)
    assert await _count(db, "chats") == 0


async def test_nul_accounting_in_aggregates(db):
    src = _source()
    src.rows["admins"][0]["name"] = "ab"  # після видалення NUL
    src.server_agg["admins"] = cp.py_aggregates(T["admins"], src.rows["admins"], {"name": 1})
    src.raw_agg["admins"] = dict(src.server_agg["admins"], nul={"name": 1})
    async with db.connect() as conn:
        tx = await conn.begin()
        assert await cp.write_and_verify(conn, src) == []
        await tx.rollback()


async def test_target_not_empty_aborts_unless_truncate(db):
    async with db.begin() as conn:
        await conn.execute(text("INSERT INTO admins (telegram_id) VALUES ('9')"))
    async with db.connect() as conn:
        with pytest.raises(cp.CopyAbort, match="not empty"):
            await cp._target_ready(conn, False)
        await conn.rollback()
        await cp._target_ready(conn, True)
        assert (await conn.execute(text("SELECT count(*) FROM admins"))).scalar_one() == 0
        await conn.rollback()
