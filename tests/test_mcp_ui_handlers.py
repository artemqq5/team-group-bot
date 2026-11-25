import asyncio
import re
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from bot.data import db_connection
from bot.data.models import AdminModel, ChatModel
from bot.data.repositories.McpAuditRepository import McpAuditRepository
from bot.data.repositories.McpKeyRepository import McpKeyRepository
from bot.domain.mcp.server import MCP_PATH
from bot.domain.routers.admin.chat.menu import mcp_keys_ as h
from bot.domain.states.McpKey import McpKeyState
from bot.presentation.keyboard import admin_ as kb

ADMIN, OTHER = 111, 222


@pytest.fixture(autouse=True)
async def admins(db):
    async with db_connection.async_session() as s:
        s.add(AdminModel(telegram_id=str(ADMIN), name="A", role="ADMIN"))
        s.add(AdminModel(telegram_id="333", name="B", role="ADMIN"))
        s.add(AdminModel(telegram_id=str(OTHER), name="O", role="ADMIN_MT"))
        await s.commit()


def fsm():
    return FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=1, user_id=1))


def cb(uid=ADMIN):
    c = AsyncMock()
    c.from_user = MagicMock(id=uid, full_name="Na<me>")
    c.message = AsyncMock()
    return c


def texts(c):
    return [a[0][0] for a in c.message.edit_text.call_args_list]


def last_text(c):
    return texts(c)[-1]


def all_cb_data(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


async def make_key(cats=("creo",), label="k"):
    secret, key = await McpKeyRepository.create(label, "1", list(cats))
    return secret, key


# ---------- access ----------
@pytest.mark.asyncio
async def test_all_handlers_deny_non_admin(monkeypatch):
    _, key = await make_key()
    st = fsm()
    bot = AsyncMock()
    calls = [
        (h.mcp_list, (st,)), (h.mcp_create_start, (st,)),
        (h.mcp_toggle_category, (kb.McpCategoryCallback(category="creo"), st)),
        (h.mcp_next, (st,)), (h.mcp_back_to_categories, (st,)), (h.mcp_confirm, (st, bot)),
        (h.mcp_hide_secret, (kb.McpKeyCallback(key_id=key.id, action="hide"),)),
        (h.mcp_view, (kb.McpKeyCallback(key_id=key.id, action="view"),)),
        (h.mcp_revoke_ask, (kb.McpKeyCallback(key_id=key.id, action="revoke"),)),
        (h.mcp_revoke, (kb.McpKeyCallback(key_id=key.id, action="revoke_yes"), bot)),
        (h.mcp_delete_ask, (kb.McpKeyCallback(key_id=key.id, action="delete"),)),
        (h.mcp_delete, (kb.McpKeyCallback(key_id=key.id, action="delete_yes"), bot)),
        (h.mcp_audit, (kb.McpAuditCallback(key_id=key.id),)),
        (h.mcp_audit_export, (kb.McpAuditCallback(key_id=key.id, export=True),)),
    ]
    for fn, args in calls:
        for uid in (OTHER, 999):  # non-ADMIN role, and non-admin
            c = cb(uid)
            await st.update_data(label="x", categories=["creo"])
            await fn(c, *args)
            c.answer.assert_awaited_once_with("Нет доступа", show_alert=True)
            c.message.edit_text.assert_not_called()
            c.message.answer.assert_not_called()
            c.message.answer_document.assert_not_called()
    assert len(await McpKeyRepository.list_keys()) == 1
    assert (await McpKeyRepository.get(key.id)).revoked is False
    bot.send_message.assert_not_called()
    # FSM label text handler
    m = AsyncMock()
    m.from_user = MagicMock(id=OTHER)
    m.text = "evil"
    await h.mcp_get_label(m, st)
    m.answer.assert_awaited_once_with("Нет доступа")
    assert await st.get_state() is None or await st.get_state() != McpKeyState.categories.state


# ---------- create flow ----------
async def run_to_confirm(st, cats):
    await st.set_state(McpKeyState.label)
    m = AsyncMock(); m.from_user = MagicMock(id=ADMIN); m.text = "  Bob  "
    await h.mcp_get_label(m, st)
    for c in cats:
        await h.mcp_toggle_category(cb(), kb.McpCategoryCallback(category=c), st)


@pytest.mark.asyncio
async def test_create_flow_end_to_end(monkeypatch):
    monkeypatch.setenv("WEBHOOK_BASE_URL", "https://x.example")
    st = fsm()
    await run_to_confirm(st, [])
    c = cb()
    await h.mcp_next(c, st)
    c.answer.assert_awaited_once_with("Выберите хотя бы одну категорию", show_alert=True)
    assert await st.get_state() == McpKeyState.categories.state
    # exclusivity
    await h.mcp_toggle_category(cb(), kb.McpCategoryCallback(category="creo"), st)
    await h.mcp_toggle_category(cb(), kb.McpCategoryCallback(category="all"), st)
    assert (await st.get_data())["categories"] == ["all"]
    await h.mcp_toggle_category(cb(), kb.McpCategoryCallback(category="apps"), st)
    assert (await st.get_data())["categories"] == ["apps"]
    await h.mcp_toggle_category(cb(), kb.McpCategoryCallback(category="bogus"), st)
    assert (await st.get_data())["categories"] == ["apps"]
    await h.mcp_next(cb(), st)
    assert await st.get_state() == McpKeyState.confirm.state

    bot = AsyncMock()
    c = cb()
    await h.mcp_confirm(c, st, bot)
    keys = await McpKeyRepository.list_keys()
    assert len(keys) == 1 and keys[0].label == "Bob" and keys[0].categories == ["apps"]
    txt = last_text(c)
    m = re.search(rf"<code>https://x\.example{MCP_PATH}/k/(tgb_[\w-]+)</code>", txt)
    secret = m.group(1)
    assert f"claude mcp add --transport http mt-chats https://x.example{MCP_PATH}/k/{secret}" in txt
    assert "--header" not in txt and "Bearer" not in txt
    key, reason = await McpKeyRepository.resolve_by_token(secret)
    assert reason is None
    # secret not in FSM storage
    assert secret not in repr(st.storage.storage)
    assert await st.get_state() is None
    # notifications: all ADMIN-role only, no secret
    sent = {a[0][0] for a in bot.send_message.call_args_list}
    assert sent == {str(ADMIN), "333"}
    assert all(secret not in a[0][1] and secret[:8] not in a[0][1] for a in bot.send_message.call_args_list)
    # hide
    c2 = cb()
    await h.mcp_hide_secret(c2, kb.McpKeyCallback(key_id=key.id, action="hide"))
    assert secret not in last_text(c2)
    # a second confirm with the stale state (filter would reject: state None)
    assert await st.get_state() != McpKeyState.confirm.state


@pytest.mark.asyncio
async def test_concurrent_double_confirm_creates_one_key():
    st = fsm()
    await run_to_confirm(st, ["creo"])
    await h.mcp_next(cb(), st)
    bot = AsyncMock()
    res = await asyncio.gather(h.mcp_confirm(cb(), st, bot), h.mcp_confirm(cb(), st, bot),
                               return_exceptions=True)
    assert len(await McpKeyRepository.list_keys()) == 1
    assert not [r for r in res if isinstance(r, Exception)], res


# ---------- revoke ----------
@pytest.mark.asyncio
async def test_revoke_requires_confirm_and_notifies():
    secret, key = await make_key()
    bot = AsyncMock()
    c = cb()
    await h.mcp_revoke_ask(c, kb.McpKeyCallback(key_id=key.id, action="revoke"))
    assert (await McpKeyRepository.get(key.id)).revoked is False
    assert (await McpKeyRepository.resolve_by_token(secret))[1] is None
    c = cb()
    await h.mcp_revoke(c, kb.McpKeyCallback(key_id=key.id, action="revoke_yes"), bot)
    assert (await McpKeyRepository.resolve_by_token(secret))[1] == "revoked"
    assert {a[0][0] for a in bot.send_message.call_args_list} == {str(ADMIN), "333"}
    # idempotent: second revoke -> no extra notifications
    n = bot.send_message.call_count
    await h.mcp_revoke(cb(), kb.McpKeyCallback(key_id=key.id, action="revoke_yes"), bot)
    assert bot.send_message.call_count == n
    # missing key
    c = cb()
    await h.mcp_revoke(c, kb.McpKeyCallback(key_id=10**9, action="revoke_yes"), bot)
    c.answer.assert_awaited_with("Ключ не найден", show_alert=True)


@pytest.mark.asyncio
async def test_notify_failure_does_not_break():
    secret, key = await make_key()
    bot = AsyncMock()
    bot.send_message.side_effect = RuntimeError("blocked")
    await h.mcp_revoke(cb(), kb.McpKeyCallback(key_id=key.id, action="revoke_yes"), bot)
    assert (await McpKeyRepository.get(key.id)).revoked is True


# ---------- delete ----------
@pytest.mark.asyncio
async def test_delete_requires_confirm_hides_notifies_keeps_audit():
    secret, key = await make_key(label="Del<me>")
    await McpAuditRepository.add(method="tools/call", status="ok", key_id=key.id)
    bot = AsyncMock()
    c = cb()
    await h.mcp_delete_ask(c, kb.McpKeyCallback(key_id=key.id, action="delete"))
    assert "Удалить ключ «Del&lt;me&gt;»? Доступ по нему пропадёт, журнал действий сохранится." in last_text(c)
    assert (await McpKeyRepository.get(key.id)).deleted_at is None
    assert (await McpKeyRepository.resolve_by_token(secret))[1] is None
    assert bot.send_message.call_count == 0
    await h.mcp_delete(cb(), kb.McpKeyCallback(key_id=key.id, action="delete_yes"), bot)
    row = await McpKeyRepository.get(key.id)
    assert row.deleted_at is not None and row.revoked and row.revoked_at and row.revoked_by == str(ADMIN)
    assert (await McpKeyRepository.resolve_by_token(secret))[1] == "revoked"
    assert await McpKeyRepository.list_keys() == []
    assert {a[0][0] for a in bot.send_message.call_args_list} == {str(ADMIN), "333"}
    assert len(await McpAuditRepository.page_by_key(key.id, 0, 10)) == 1
    # повторне підтвердження: без другого сповіщення; видалений ключ — "не знайдено"
    n = bot.send_message.call_count
    c = cb()
    await h.mcp_delete(c, kb.McpKeyCallback(key_id=key.id, action="delete_yes"), bot)
    c.answer.assert_awaited_with("Ключ не найден", show_alert=True)
    assert bot.send_message.call_count == n
    assert await McpKeyRepository.delete(key.id, "1") is False
    # журнал показує мітку і «(удалён)»
    c = cb()
    await h.mcp_audit(c, kb.McpAuditCallback(key_id=key.id))
    assert "Del&lt;me&gt;" in last_text(c) and "(удалён)" in last_text(c)


@pytest.mark.asyncio
async def test_delete_revoked_key_keeps_original_revoke_info():
    _, key = await make_key()
    await McpKeyRepository.revoke(key.id, "777")
    assert await McpKeyRepository.delete(key.id, "888") is True
    row = await McpKeyRepository.get(key.id)
    assert row.revoked_by == "777" and row.deleted_at is not None


def test_key_card_has_delete_button():
    class K: id = 5; revoked = True
    assert any(d.startswith("mck:") and d.endswith(":delete") for d in all_cb_data(kb.kb_mcp_key_view(K)))


# ---------- audit ----------
async def seed_audit(n, key_id, **kw):
    base = datetime(2026, 1, 1)
    async with db_connection.async_session() as s:
        from bot.data.models import McpAuditLogModel
        for i in range(n):
            s.add(McpAuditLogModel(created_at=base + timedelta(minutes=i), key_id=key_id, method="tools/call",
                                   tool_name=f"tool{i:02d}", arguments={"query": f"q{i}"}, status="ok",
                                   hits=kw.get("hits", 1), result_count=i))
        await s.commit()


@pytest.mark.asyncio
async def test_audit_pagination():
    _, key = await make_key()
    await seed_audit(25, key.id)
    seen = []
    for page, n in ((1, 10), (2, 10), (3, 5)):
        c = cb()
        await h.mcp_audit(c, kb.McpAuditCallback(key_id=key.id, page=page))
        t = last_text(c)
        names = re.findall(r"tool\d\d", t)
        assert len(names) == n
        seen += names
        data = all_cb_data(c.message.edit_text.call_args[1]["reply_markup"])
        has_next = any(d.startswith("mca:") and f":{page + 1}:" in d for d in data)
        assert has_next == (page < 3)
    assert len(seen) == 25 == len(set(seen))
    # page 0 / negative clamp
    c = cb(); await h.mcp_audit(c, kb.McpAuditCallback(key_id=key.id, page=-5))
    assert len(re.findall(r"tool\d\d", last_text(c))) == 10
    # beyond last page
    c = cb(); await h.mcp_audit(c, kb.McpAuditCallback(key_id=key.id, page=99))
    assert "Записей нет" in last_text(c)


@pytest.mark.asyncio
async def test_audit_hits_and_unknown():
    _, key = await make_key()
    await seed_audit(1, key.id, hits=5)
    await seed_audit(1, None, hits=1)
    c = cb(); await h.mcp_audit(c, kb.McpAuditCallback(key_id=key.id))
    assert "×5" in last_text(c)
    c = cb(); await h.mcp_audit(c, kb.McpAuditCallback(key_id=0))
    assert "×" not in last_text(c) and "tool00" in last_text(c)


@pytest.mark.asyncio
async def test_audit_html_escape():
    _, key = await make_key()
    await McpAuditRepository.add("tools/call", "ok", key_id=key.id, tool_name="t",
                                 arguments={"query": "<b>&x", "chat_id": "<i>"}, client_ip="<u>")
    c = cb(); await h.mcp_audit(c, kb.McpAuditCallback(key_id=key.id))
    t = last_text(c)
    assert "<b>&x" not in t and "<i>" not in t and "<u>" not in t


@pytest.mark.asyncio
async def test_csv_export_escapes_formulas():
    _, key = await make_key()
    for v in ("=1+1", "+1", "-1", "@SUM(A1)"):
        await McpAuditRepository.add("tools/call", "ok", key_id=key.id, tool_name=v, client_ip=v)
    c = cb()
    await h.mcp_audit_export(c, kb.McpAuditCallback(key_id=key.id, export=True))
    doc = c.message.answer_document.call_args[0][0]
    txt = doc.data.decode("utf-8-sig")
    for v in ("=1+1", "+1", "-1", "@SUM(A1)"):
        assert f",'{v}," in txt or f",'{v}\r" in txt, txt
        assert f",{v}," not in txt
    # forged export for missing key: no crash
    c = cb(); await h.mcp_audit_export(c, kb.McpAuditCallback(key_id=10**9, export=True))


# ---------- keyboards / strings ----------
def test_callback_data_len():
    big = 10**9
    items = [
        kb.McpMenuCallback(action="revoke_yes_longest"), kb.McpCategoryCallback(category="affiliate_mp"),
        kb.McpKeyCallback(key_id=big, action="revoke_yes"),
        kb.McpAuditCallback(key_id=big, page=10**5, export=True),
    ]
    for i in items:
        assert len(i.pack().encode()) <= 64, i.pack()
    k = MagicMock(id=big, revoked=False, label="L", token_prefix="tgb_abcd")
    for m in (kb.kb_mcp_keys([k]), kb.kb_mcp_key_view(k), kb.kb_mcp_secret(big),
              kb.kb_mcp_revoke_confirm(big), kb.kb_mcp_audit(big, 10**5, True),
              kb.kb_mcp_categories(["all"]), kb.kb_mcp_confirm()):
        for d in all_cb_data(m):
            assert len(d.encode()) <= 64, d


def test_no_ukrainian_letters_in_new_code():
    import inspect, ast
    from bot.domain.routers.admin.chat.menu import show_, statistic_
    from bot.domain.routers.admin.chat import main_chat
    ukr = re.compile("[ІіЇїЄєҐґ]")
    for mod in (h, show_, statistic_, main_chat, kb):
        tree = ast.parse(inspect.getsource(mod))
        docs = {id(n.body[0].value) for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module, ast.ClassDef))
                and n.body and isinstance(n.body[0], ast.Expr)}
        for n in ast.walk(tree):
            if id(n) in docs:
                continue
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and ukr.search(n.value):
                pytest.fail(f"{mod.__name__}:{n.lineno} {n.value!r}")


def test_kb_main_mcp_button_admin_only():
    a = kb.kb_main(MagicMock(role="ADMIN")); b = kb.kb_main(MagicMock(role="ADMIN_MT"))
    assert any(d.startswith("mcp:") for d in all_cb_data(a))
    assert not any(d.startswith("mcp:") for d in all_cb_data(b))


# ---------- show_/statistic_ ----------
@pytest.mark.asyncio
async def test_stats_and_group_info_no_messages_and_silent_cap():
    from bot.domain.routers.admin.chat.menu import show_, statistic_
    async with db_connection.async_session() as s:
        for i in range(45):
            s.add(ChatModel(group_id=f"g{i:02d}", title=f"<b>T{i:02d}</b>"))
        await s.commit()
    c = cb()
    await statistic_.statistic_info(c)
    t = last_text(c)
    assert "захвачено за 24 ч:</b> 0" in t
    assert "(45)" in t and "… и ещё 15" in t and "<b>T00</b>" not in t and "&lt;b&gt;T00" in t
    assert t.count("&lt;b&gt;T") == 30
    c = cb(OTHER); await statistic_.statistic_info(c)
    c.answer.assert_awaited_once_with("Нет доступа", show_alert=True)
    c = cb()
    await show_.about_group(c, kb.GroupCallback(id="g00"))
    assert "Сообщений за 24 ч:</b> 0 · последнее: —" in last_text(c)
