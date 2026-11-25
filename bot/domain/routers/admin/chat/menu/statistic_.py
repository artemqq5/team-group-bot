from datetime import datetime, timedelta
from html import escape

from aiogram import Router, F
from aiogram.types import CallbackQuery

from bot.data.other.accesses import TypeOfAdmins
from bot.data.repositories.AdminRepository import AdminRepository
from bot.data.repositories.ChatRepository import ChatRepository
from bot.data.repositories.MessageRepository import MessageStats
from bot.data.repositories.UserRepository import UserRepository
from bot.domain.tools.edit_helper import edit_or_answer
from bot.presentation.keyboard.admin_ import MainMenuCallback, kb_back_to_main, CATEGORY_LABELS

router = Router()

SILENT_LIST_CAP = 30


@router.callback_query(MainMenuCallback.filter(F.action == "stats"))
async def statistic_info(callback: CallbackQuery):
    admin = await AdminRepository.is_admin(callback.from_user.id)
    if admin is None or admin.role != TypeOfAdmins.ADMIN.value:
        await callback.answer("Нет доступа", show_alert=True)
        return

    chats = await ChatRepository.all_chats() or []
    users_count = await UserRepository.count_users_total() or 0
    admins_list = await AdminRepository.get_all_admins() or []
    cat_counts = await ChatRepository.count_chats_by_category() or {}
    top_groups = await UserRepository.top_groups_by_users(5) or []

    cat_lines = []
    for key, label in CATEGORY_LABELS.items():
        cat_lines.append(f"  {label}: {cat_counts.get(key, 0)}")
    cat_lines.append(f"  Неопределённые: {cat_counts.get('unspecified', 0)}")

    top_lines = []
    for i, row in enumerate(top_groups, 1):
        title = escape(str(row.title_group or row.group_id))
        top_lines.append(f"  {i}. {title} — {row.cnt} юзеров")

    now = datetime.utcnow()
    msgs_24h = await MessageStats.count_since(now - timedelta(hours=24)) or 0
    silent = await MessageStats.silent_groups(now - timedelta(days=3)) or []
    silent_lines = [f"  {escape(str(title or gid))}" for gid, title in silent[:SILENT_LIST_CAP]]
    if len(silent) > SILENT_LIST_CAP:
        silent_lines.append(f"  … и ещё {len(silent) - SILENT_LIST_CAP}")

    info = (
        "<b>Статистика по боту</b>\n\n"
        f"Всего групп: <b>{len(chats)}</b>\n"
        f"Всего юзеров: <b>{users_count}</b>\n"
        f"Всего админов: <b>{len(admins_list)}</b>\n\n"
        "<b>Группы по категориям:</b>\n"
        + "\n".join(cat_lines)
        + ("\n\n<b>Топ-5 групп по юзерам:</b>\n" + "\n".join(top_lines) if top_lines else "")
        + f"\n\n<b>Сообщений захвачено за 24 ч:</b> {msgs_24h}"
        + (f"\n\n<b>Группы без сообщений за 3 дня ({len(silent)}):</b>\n" + "\n".join(silent_lines)
           if silent_lines else "")
    )
    await edit_or_answer(callback, info, reply_markup=kb_back_to_main())
    await callback.answer()
