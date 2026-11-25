import csv
import io
import logging
import os
from html import escape

from aiogram import Router, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, BufferedInputFile

from bot.data.other.accesses import MCP_KEY_ROLES, TypeOfChats
from bot.data.repositories.AdminRepository import AdminRepository
from bot.data.repositories.McpAuditRepository import McpAuditRepository
from bot.data.repositories.McpKeyRepository import McpKeyRepository
from bot.domain.mcp.server import MCP_PATH
from bot.domain.states.McpKey import McpKeyState
from bot.domain.tools.edit_helper import edit_or_answer
from bot.presentation.keyboard.admin_ import (
    McpMenuCallback, McpCategoryCallback, McpKeyCallback, McpAuditCallback, CATEGORY_LABELS,
    kb_cancel, kb_mcp_keys, kb_mcp_categories, kb_mcp_confirm, kb_mcp_secret, kb_mcp_key_view,
    kb_mcp_revoke_confirm, kb_mcp_audit, kb_mcp_delete_confirm
)

router = Router()

AUDIT_PAGE_SIZE = 10
CONFIRM_WARNING = (
    "Доступ проверяется в момент запроса: если группе позже добавят категорию, "
    "ключ увидит и её прошлую историю. Ключ «Все группы» увидит и новые группы."
)


async def _admin_or_deny(callback: CallbackQuery):
    """Each handler re-checks the role: FSM state and old buttons grant no access."""
    admin = await AdminRepository.is_admin(callback.from_user.id)
    if admin is None or admin.role not in MCP_KEY_ROLES:
        await callback.answer("Нет доступа", show_alert=True)
        return None
    return admin


def _connector_url() -> str:
    return f"{os.getenv('WEBHOOK_BASE_URL', '').rstrip('/')}{MCP_PATH}"


def _fmt_dt(dt) -> str:
    return dt.strftime("%Y-%m-%d %H:%M") if dt else "—"


def _cats_text(categories) -> str:
    if TypeOfChats.ALL.value in categories:
        return "Все группы"
    names = [CATEGORY_LABELS[c] for c in CATEGORY_LABELS if c in categories]
    return ", ".join(names) if names else "—"


def _actor(callback: CallbackQuery) -> str:
    return f"{escape(callback.from_user.full_name)} (<code>{callback.from_user.id}</code>)"


async def _notify_admins(bot: Bot, text: str):
    """Секрет у сповіщення не потрапляє ніколи."""
    for admin in await AdminRepository.get_admins() or []:
        try:
            await bot.send_message(admin.telegram_id, text)
        except Exception as e:
            logging.warning(f"[mcp_keys] notify {admin.telegram_id} failed: {e}")


# ─── Список ключей ────────────────────────────────────────────────────────────

@router.callback_query(McpMenuCallback.filter(F.action == "list"))
async def mcp_list(callback: CallbackQuery, state: FSMContext):
    if not await _admin_or_deny(callback):
        return
    await state.clear()
    keys = await McpKeyRepository.list_keys() or []
    lines = [
        f"{i}. <b>{escape(k.label)}</b> · <code>{escape(k.token_prefix)}…</code> · "
        f"{'отозван' if k.revoked else 'активен'}"
        for i, k in enumerate(keys, 1)
    ]
    text = "🔑 <b>Ключи MCP</b>\n\n" + ("\n".join(lines) if lines else "Ключей пока нет.")
    await edit_or_answer(callback, text, reply_markup=kb_mcp_keys(keys))
    await callback.answer()


# ─── Создание ключа ───────────────────────────────────────────────────────────

@router.callback_query(McpMenuCallback.filter(F.action == "create"))
async def mcp_create_start(callback: CallbackQuery, state: FSMContext):
    if not await _admin_or_deny(callback):
        return
    await state.clear()
    await state.set_state(McpKeyState.label)
    await edit_or_answer(callback, "Для кого ключ? Введите название:", reply_markup=kb_cancel())
    await callback.answer()


@router.message(McpKeyState.label)
async def mcp_get_label(message: Message, state: FSMContext):
    admin = await AdminRepository.is_admin(message.from_user.id)
    if admin is None or admin.role not in MCP_KEY_ROLES:
        await message.answer("Нет доступа")
        return
    label = (message.text or "").strip()
    if not label or len(label) > 255:
        await message.answer("Введите название (до 255 символов):", reply_markup=kb_cancel())
        return
    await state.update_data(label=label, categories=[])
    await state.set_state(McpKeyState.categories)
    await message.answer("Выберите категории групп, которые сможет читать ключ:", reply_markup=kb_mcp_categories([]))


@router.callback_query(McpCategoryCallback.filter(), McpKeyState.categories)
async def mcp_toggle_category(callback: CallbackQuery, callback_data: McpCategoryCallback, state: FSMContext):
    if not await _admin_or_deny(callback):
        return
    cat = callback_data.category
    if cat != TypeOfChats.ALL.value and cat not in CATEGORY_LABELS:
        await callback.answer()
        return
    selected = list((await state.get_data()).get("categories", []))
    if cat == TypeOfChats.ALL.value:
        selected = [] if cat in selected else [cat]
    else:
        selected = [c for c in selected if c != TypeOfChats.ALL.value]
        selected = [c for c in selected if c != cat] if cat in selected else selected + [cat]
    await state.update_data(categories=selected)
    await callback.message.edit_reply_markup(reply_markup=kb_mcp_categories(selected))
    await callback.answer()


async def _show_categories(callback: CallbackQuery, state: FSMContext):
    selected = (await state.get_data()).get("categories", [])
    await edit_or_answer(
        callback, "Выберите категории групп, которые сможет читать ключ:",
        reply_markup=kb_mcp_categories(selected)
    )


@router.callback_query(McpMenuCallback.filter(F.action == "next"), McpKeyState.categories)
async def mcp_next(callback: CallbackQuery, state: FSMContext):
    if not await _admin_or_deny(callback):
        return
    data = await state.get_data()
    if not data.get("categories"):
        await callback.answer("Выберите хотя бы одну категорию", show_alert=True)
        return
    await state.set_state(McpKeyState.confirm)
    text = (
        "<b>Новый ключ MCP</b>\n\n"
        f"Название: <b>{escape(data['label'])}</b>\n"
        f"Категории: {escape(_cats_text(data['categories']))}\n\n"
        f"⚠️ {CONFIRM_WARNING}"
    )
    await edit_or_answer(callback, text, reply_markup=kb_mcp_confirm())
    await callback.answer()


@router.callback_query(McpMenuCallback.filter(F.action == "back_cats"), McpKeyState.confirm)
async def mcp_back_to_categories(callback: CallbackQuery, state: FSMContext):
    if not await _admin_or_deny(callback):
        return
    await state.set_state(McpKeyState.categories)
    await _show_categories(callback, state)
    await callback.answer()


@router.callback_query(McpMenuCallback.filter(F.action == "confirm"), McpKeyState.confirm)
async def mcp_confirm(callback: CallbackQuery, state: FSMContext, bot: Bot):
    # дані читаємо і стан чистимо до будь-якого іншого await: паралельне натискання не створить другий ключ
    data = await state.get_data()
    await state.clear()
    if "label" not in data:
        await callback.answer()
        return
    if not await _admin_or_deny(callback):
        return
    created = await McpKeyRepository.create(data["label"], str(callback.from_user.id), data["categories"])
    if not created:
        await edit_or_answer(callback, "Не удалось создать ключ. Попробуйте позже.")
        await callback.answer()
        return
    secret, key = created
    text = (
        f"✅ Ключ <b>{escape(key.label)}</b> создан.\n\n"
        f"Ключ (показан один раз):\n<code>{escape(secret)}</code>\n\n"
        f"Адрес коннектора:\n<code>{escape(_connector_url())}</code>\n\n"
        "Claude (сайт/приложение): Настройки → Коннекторы → Добавить свой коннектор → вставить адрес. "
        "Откроется страница входа — вставьте туда ключ. "
        "Для Team/Enterprise: Organization settings → Connectors (владелец).\n\n"
        "⚠️ Ключ вводится только на странице входа, в адрес его вставлять не нужно. "
        "Не пересылайте ключ; при утечке отзовите его в боте.\n"
        "Сохраните ключ сейчас: повторно он не показывается."
    )
    await edit_or_answer(callback, text, reply_markup=kb_mcp_secret(key.id))
    await callback.answer()
    await _notify_admins(
        bot,
        f"🔑 Создан ключ MCP <b>{escape(key.label)}</b>\n"
        f"Категории: {escape(_cats_text(key.categories))}\n"
        f"Создал: {_actor(callback)}"
    )


@router.callback_query(McpKeyCallback.filter(F.action == "hide"))
async def mcp_hide_secret(callback: CallbackQuery, callback_data: McpKeyCallback):
    if not await _admin_or_deny(callback):
        return
    key = await McpKeyRepository.get(callback_data.key_id)
    name = escape(key.label) if key else ""
    await edit_or_answer(
        callback, f"✅ Ключ <b>{name}</b> сохранён, секрет скрыт.",
        reply_markup=kb_mcp_key_view(key) if key else None
    )
    await callback.answer()


# ─── Просмотр, отзыв ──────────────────────────────────────────────────────────

@router.callback_query(McpKeyCallback.filter(F.action == "view"))
async def mcp_view(callback: CallbackQuery, callback_data: McpKeyCallback):
    if not await _admin_or_deny(callback):
        return
    key = await McpKeyRepository.get(callback_data.key_id)
    if not key or key.deleted_at:
        await callback.answer("Ключ не найден", show_alert=True)
        return
    lines = [
        f"🔑 <b>{escape(key.label)}</b> · <code>{escape(key.token_prefix)}…</code>",
        f"Статус: {'отозван' if key.revoked else 'активен'}",
        f"Создал: <code>{escape(key.created_by)}</code>",
        f"Создан: {_fmt_dt(key.created_at)} UTC",
        f"Категории: {escape(_cats_text(key.categories))}",
        f"Адрес коннектора: <code>{escape(_connector_url())}</code>",
        f"Последнее использование: {_fmt_dt(key.last_used_at)}" + (" UTC" if key.last_used_at else ""),
    ]
    if key.revoked:
        lines.append(f"Отозван: {_fmt_dt(key.revoked_at)} UTC (<code>{escape(str(key.revoked_by))}</code>)")
    await edit_or_answer(callback, "\n".join(lines), reply_markup=kb_mcp_key_view(key))
    await callback.answer()


@router.callback_query(McpKeyCallback.filter(F.action == "revoke"))
async def mcp_revoke_ask(callback: CallbackQuery, callback_data: McpKeyCallback):
    if not await _admin_or_deny(callback):
        return
    key = await McpKeyRepository.get(callback_data.key_id)
    if not key or key.deleted_at:
        await callback.answer("Ключ не найден", show_alert=True)
        return
    await edit_or_answer(
        callback,
        f"Отозвать ключ <b>{escape(key.label)}</b>? Он сразу перестанет работать.",
        reply_markup=kb_mcp_revoke_confirm(key.id)
    )
    await callback.answer()


@router.callback_query(McpKeyCallback.filter(F.action == "revoke_yes"))
async def mcp_revoke(callback: CallbackQuery, callback_data: McpKeyCallback, bot: Bot):
    if not await _admin_or_deny(callback):
        return
    key = await McpKeyRepository.get(callback_data.key_id)
    if not key or key.deleted_at:
        await callback.answer("Ключ не найден", show_alert=True)
        return
    revoked = await McpKeyRepository.revoke(key.id, str(callback.from_user.id))
    await callback.answer("Ключ отозван" if revoked else "Ключ уже отозван")
    if revoked:
        await _notify_admins(
            bot,
            f"🚫 Отозван ключ MCP <b>{escape(key.label)}</b>\n"
            f"Категории: {escape(_cats_text(key.categories))}\n"
            f"Отозвал: {_actor(callback)}"
        )
    key = await McpKeyRepository.get(key.id)
    if key:
        await edit_or_answer(
            callback, f"🚫 Ключ <b>{escape(key.label)}</b> отозван.", reply_markup=kb_mcp_key_view(key)
        )


@router.callback_query(McpKeyCallback.filter(F.action == "delete"))
async def mcp_delete_ask(callback: CallbackQuery, callback_data: McpKeyCallback):
    if not await _admin_or_deny(callback):
        return
    key = await McpKeyRepository.get(callback_data.key_id)
    if not key or key.deleted_at:
        await callback.answer("Ключ не найден", show_alert=True)
        return
    await edit_or_answer(
        callback,
        f"Удалить ключ «{escape(key.label)}»? Доступ по нему пропадёт, журнал действий сохранится.",
        reply_markup=kb_mcp_delete_confirm(key.id)
    )
    await callback.answer()


@router.callback_query(McpKeyCallback.filter(F.action == "delete_yes"))
async def mcp_delete(callback: CallbackQuery, callback_data: McpKeyCallback, bot: Bot):
    if not await _admin_or_deny(callback):
        return
    key = await McpKeyRepository.get(callback_data.key_id)
    if not key or key.deleted_at:
        await callback.answer("Ключ не найден", show_alert=True)
        return
    # атомарний UPDATE: повторне натискання не дасть другого сповіщення
    deleted = await McpKeyRepository.delete(key.id, str(callback.from_user.id))
    await callback.answer("Ключ удалён" if deleted else "Ключ уже удалён")
    if deleted:
        await _notify_admins(
            bot,
            f"🗑 Удалён ключ MCP <b>{escape(key.label)}</b>\n"
            f"Категории: {escape(_cats_text(key.categories))}\n"
            f"Удалил: {_actor(callback)}"
        )
        await edit_or_answer(
            callback, f"🗑 Ключ <b>{escape(key.label)}</b> удалён. Журнал действий сохранён.",
            reply_markup=kb_mcp_keys(await McpKeyRepository.list_keys() or [])
        )


# ─── Журнал ───────────────────────────────────────────────────────────────────

def _short(value, n: int = 30) -> str:
    s = str(value)
    return s if len(s) <= n else s[:n] + "…"


def _audit_line(row) -> str:
    args = row.arguments if isinstance(row.arguments, dict) else {}
    what = row.tool_name or row.method
    parts = [f"<code>{_fmt_dt(row.created_at)}</code> {escape(str(what))}"]
    if args.get("chat_id"):
        parts.append(f"чат {escape(_short(args['chat_id'], 20))}")
    if args.get("since") or args.get("until"):
        parts.append(escape(f"{_short(args.get('since') or '…', 16)}..{_short(args.get('until') or '…', 16)}"))
    if args.get("query"):
        parts.append(f"«{escape(_short(args['query']))}»")
    if row.result_count is not None:
        parts.append(f"результатов: {row.result_count}")
    status = row.status + (f"/{row.reason}" if row.reason else "")
    parts.append(escape(status))
    if row.hits and row.hits > 1:
        parts.append(f"×{row.hits}")
    if row.client_ip:
        parts.append(escape(row.client_ip))
    return " · ".join(parts)


@router.callback_query(McpAuditCallback.filter(F.export == False))  # noqa: E712
async def mcp_audit(callback: CallbackQuery, callback_data: McpAuditCallback):
    if not await _admin_or_deny(callback):
        return
    key_id, page = callback_data.key_id, max(callback_data.page, 1)
    offset = (page - 1) * AUDIT_PAGE_SIZE
    # limit+1: так дізнаємось, чи є наступна сторінка
    if key_id:
        rows = await McpAuditRepository.page_by_key(key_id, offset, AUDIT_PAGE_SIZE + 1)
        k = await McpKeyRepository.get(key_id)
        title = "📜 Журнал ключа" + (f" «{escape(k.label)}»" + (" (удалён)" if k.deleted_at else "") if k else "")
    else:
        rows = await McpAuditRepository.page_unknown(offset, AUDIT_PAGE_SIZE + 1)
        title = "🚫 Отказы без ключа"
    rows = rows or []
    has_next = len(rows) > AUDIT_PAGE_SIZE
    body = "\n\n".join(_audit_line(r) for r in rows[:AUDIT_PAGE_SIZE]) or "Записей нет."
    await edit_or_answer(
        callback, f"<b>{title}</b> (время UTC, сначала новые)\n\n{body}",
        reply_markup=kb_mcp_audit(key_id, page, has_next)
    )
    await callback.answer()


def _csv_cell(value):
    # захист від формул в Excel: значення приходять від клієнта ключа
    s = "" if value is None else str(value)
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s


@router.callback_query(McpAuditCallback.filter(F.export == True))  # noqa: E712
async def mcp_audit_export(callback: CallbackQuery, callback_data: McpAuditCallback):
    if not await _admin_or_deny(callback):
        return
    rows = await McpAuditRepository.export_rows(callback_data.key_id)
    if rows is None:
        await callback.answer("Не удалось получить журнал", show_alert=True)
        return
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["created_at_utc", "method", "tool", "arguments", "status", "reason",
                     "result_count", "hits", "client_ip"])
    for r in rows:
        writer.writerow([_csv_cell(v) for v in (
            r.created_at, r.method, r.tool_name, r.arguments, r.status, r.reason,
            r.result_count, r.hits, r.client_ip)])
    file = BufferedInputFile(buf.getvalue().encode("utf-8-sig"), filename=f"mcp_audit_key_{callback_data.key_id}.csv")
    await callback.message.answer_document(file, caption=f"Журнал ключа, записей: {len(rows)}")
    await callback.answer()
