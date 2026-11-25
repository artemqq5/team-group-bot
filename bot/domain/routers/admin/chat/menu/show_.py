from datetime import datetime, timedelta

from aiogram import Router, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from aiogram.utils.text_decorations import html_decoration as hd

from bot.data.other.accesses import access_admin_to_chat, TypeOfChats
from bot.data.repositories.AdminRepository import AdminRepository
from bot.data.repositories.ChatRepository import ChatRepository
from bot.data.repositories.MessageRepository import MessageStats
from bot.data.repositories.UserRepository import UserRepository
from bot.domain.tools.edit_helper import edit_or_answer
from bot.presentation.keyboard.admin_ import (
    MainMenuCallback, ShowTypeCallback, GroupCallback, GroupPageCallback,
    GroupSettingsCallback, GroupToggleCallback, GroupDeleteCallback,
    kb_type_group, generate_pagination_groups, kb_group_info, kb_group_settings,
    kb_group_delete_confirm, kb_back_to_main, CATEGORY_LABELS
)

router = Router()


@router.callback_query(MainMenuCallback.filter(F.action == "groups"))
async def show_type_selection(callback: CallbackQuery):
    admin = await AdminRepository.is_admin(callback.from_user.id)
    await edit_or_answer(callback, "Выберите тип группы:", reply_markup=kb_type_group(admin))
    await callback.answer()


@router.callback_query(ShowTypeCallback.filter())
async def show_groups_by_type(callback: CallbackQuery, callback_data: ShowTypeCallback, state: FSMContext):
    admin = await AdminRepository.is_admin(callback.from_user.id)
    chat_type = callback_data.chat_type

    if chat_type not in access_admin_to_chat.get(admin.role, []) and chat_type != "unspecified":
        await callback.answer("Нет доступа", show_alert=True)
        return

    if chat_type == "unspecified":
        chats = await ChatRepository.unspecified_chats()
        title = "Неопределённые группы"
    elif chat_type == TypeOfChats.ALL.value:
        chats = await ChatRepository.all_chats()
        title = "Все группы"
    else:
        chats = await ChatRepository.chat_by_type(chat_type)
        title = chat_type

    chats = chats or []
    await state.update_data(chats=chats, list_type=chat_type)

    if not chats:
        await edit_or_answer(
            callback,
            f"По типу <b>{title}</b> групп не найдено.",
            reply_markup=kb_back_to_main()
        )
        await callback.answer()
        return

    await edit_or_answer(
        callback,
        f"Группы по типу <b>{title}</b> ({len(chats)}):",
        reply_markup=generate_pagination_groups(
            current_page=1, groups=chats, bulk_button=chat_type == "unspecified"
        )
    )
    await callback.answer()


@router.callback_query(GroupPageCallback.filter())
async def page_group_listener(callback: CallbackQuery, callback_data: GroupPageCallback, state: FSMContext):
    data = await state.get_data()
    chats = data.get("chats", [])
    await callback.message.edit_reply_markup(
        reply_markup=generate_pagination_groups(
            current_page=callback_data.page, groups=chats,
            bulk_button=data.get("list_type") == "unspecified"
        )
    )
    await callback.answer()


@router.callback_query(F.data == "page_noop")
async def noop_page_indicator(callback: CallbackQuery):
    await callback.answer()


@router.callback_query(GroupCallback.filter())
async def about_group(callback: CallbackQuery, callback_data: GroupCallback):
    group = await ChatRepository.get_chat(callback_data.id)
    if not group:
        await callback.answer("Группа не найдена", show_alert=True)
        return

    user_count = await UserRepository.count_users_in_group(callback_data.id)
    activity = await MessageStats.group_activity(callback_data.id, datetime.utcnow() - timedelta(hours=24))
    msgs_24h, last_msg = activity if activity else (0, None)
    last_text = f"{last_msg:%Y-%m-%d %H:%M} UTC" if last_msg else "—"

    type_cols = [t.value for t in TypeOfChats if t != TypeOfChats.ALL]
    categories = [CATEGORY_LABELS.get(col, col) for col in type_cols if getattr(group, col)]
    categories_text = "\n".join(categories) if categories else "Неопределённая группа"

    info = (
        f"<b>{hd.quote(group.title)}</b> (<code>{group.group_id}</code>)\n\n"
        f"<b>Категории:</b>\n{categories_text}\n\n"
        f"<b>Участников отслеживается:</b> {user_count}\n"
        f"<b>Сообщений за 24 ч:</b> {msgs_24h} · последнее: {last_text}\n"
        f"<b>Ссылка:</b> {group.link or '—'}\n"
        f"Добавлена: {group.time}"
    )
    await edit_or_answer(callback, info, reply_markup=kb_group_info(str(group.group_id)))
    await callback.answer()


@router.callback_query(GroupSettingsCallback.filter())
async def show_group_settings(callback: CallbackQuery, callback_data: GroupSettingsCallback):
    group = await ChatRepository.get_chat(callback_data.group_id)
    if not group:
        await callback.answer("Группа не найдена", show_alert=True)
        return
    await edit_or_answer(
        callback,
        f"⚙️ Настройки группы <b>{hd.quote(group.title)}</b>:",
        reply_markup=kb_group_settings(group)
    )
    await callback.answer()


@router.callback_query(GroupToggleCallback.filter())
async def toggle_group_category(callback: CallbackQuery, callback_data: GroupToggleCallback):
    group = await ChatRepository.get_chat(callback_data.group_id)
    if not group:
        await callback.answer("Группа не найдена", show_alert=True)
        return

    current_value = getattr(group, callback_data.category, False)
    await ChatRepository.update_chat_type(callback_data.group_id, callback_data.category, not current_value)

    updated = await ChatRepository.get_chat(callback_data.group_id)
    await callback.message.edit_reply_markup(reply_markup=kb_group_settings(updated))
    await callback.answer()


@router.callback_query(GroupDeleteCallback.filter(F.confirmed == False))
async def ask_delete_group(callback: CallbackQuery, callback_data: GroupDeleteCallback):
    group = await ChatRepository.get_chat(callback_data.group_id)
    name = group.title if group else callback_data.group_id
    await edit_or_answer(
        callback,
        f"Удалить группу <b>{hd.quote(name)}</b> из базы?",
        reply_markup=kb_group_delete_confirm(callback_data.group_id)
    )
    await callback.answer()


@router.callback_query(GroupDeleteCallback.filter(F.confirmed == True))
async def confirm_delete_group(callback: CallbackQuery, callback_data: GroupDeleteCallback):
    ok = await ChatRepository.remove_chat(callback_data.group_id)
    text = "✅ Группа удалена." if ok else "❌ Ошибка при удалении."
    await edit_or_answer(callback, text, reply_markup=kb_back_to_main())
    await callback.answer()
